// Run with PGLITE_MODULE pointing to an installed @electric-sql/pglite module.
// Uses an isolated PostgreSQL instance; never connects to production.
import { readFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
const { PGlite } = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite')
const db = new PGlite()
const admin = '00000000-0000-0000-0000-000000000001'
const target = '00000000-0000-0000-0000-000000000002'
const other = '00000000-0000-0000-0000-000000000003'
let request = 10
const id = () => `00000000-0000-0000-0000-${String(request++).padStart(12, '0')}`
await db.exec(`
create role anon;
create role authenticated;
create role service_role bypassrls;
create schema auth;
create function auth.uid() returns uuid language sql stable as
$$ select nullif(current_setting('test.uid', true), '')::uuid $$;
grant usage on schema auth to authenticated, service_role;
create table public.nutris (
 id uuid primary key, role text default 'user', is_suspended boolean default false,
 subscription_type text default 'free_trial', subscription_status text default 'active',
 subscription_start date, subscription_end date,
 max_reports_month integer default 15, reports_this_month integer default 0,
 reports_total integer default 0, reports_month_reset date
);
grant select on public.nutris to authenticated;
grant all on public.nutris to service_role;
alter table public.nutris enable row level security;
create policy own on public.nutris for select to authenticated using (id = auth.uid());
insert into public.nutris(id,role,subscription_end) values
 ('${admin}','admin',current_date),('${target}','user',current_date - 5),('${other}','user',current_date);
`)
await db.exec(await readFile(new URL('../migrations/2026_06_25_cupo_reset_aniversario.sql', import.meta.url), 'utf8'))
await db.exec(await readFile(new URL('../migrations/20261013114739_admin_account_extensions.sql', import.meta.url), 'utf8'))
async function row() { return (await db.query('select * from nutris where id=$1', [target])).rows[0] }
async function gate() { return (await db.query('select can_generate_report($1) as result', [target])).rows[0].result }
async function extend({ actor=admin, who=target, req=id(), days=15, extra=0, end }={}) {
 end ??= (await row()).subscription_end.toISOString().slice(0,10)
 return (await db.query('select admin_extend_account($1,$2,$3,$4,$5,$6) as result', [actor,who,req,days,extra,end])).rows[0].result
}
const dateOffset = async days => (await db.query('select (current_date + $1::integer)::text as d', [days])).rows[0].d
const initial = await row()
const firstRequest=id(), oldEnd=await dateOffset(-5)
let result = await extend({req:firstRequest,extra:3})
assert.equal(result.subscription_end,await dateOffset(15))
assert.equal((await row()).subscription_type,'free_trial')
assert.equal((await row()).max_reports_month,15)
assert.equal((await row()).reports_this_month,initial.reports_this_month)
assert.equal((await gate()).remaining,18)
assert.equal((await extend({req:firstRequest,extra:3,end:oldEnd})).already_applied,true)
assert.equal((await gate()).remaining,18)
assert.equal((await extend({req:firstRequest,extra:4,end:oldEnd})).reason,'request_conflict')
assert.equal((await extend({end:oldEnd})).reason,'changed')
assert.equal((await extend({days:30})).subscription_end,await dateOffset(45))
assert.equal((await gate()).remaining,18)
for (let i=0;i<15;i++) await db.query('select increment_report_count($1)',[target])
assert.deepEqual(await gate(),{ok:true,used:15,limit:18,remaining:3,extra_remaining:3})
for (let i=0;i<3;i++) await db.query('select increment_report_count($1)',[target])
assert.equal((await gate()).ok,false)
assert.equal((await row()).reports_total,18)
assert.equal((await extend({actor:other})).reason,'forbidden')
assert.equal((await extend({who:admin})).reason,'admin_account')
assert.equal((await extend({days:1})).reason,'invalid_input')
assert.equal((await extend({extra:-1})).reason,'invalid_input')
await db.query('update nutris set is_suspended=true where id=$1',[target])
assert.equal((await extend()).reason,'suspended')
assert.equal((await gate()).reason,'subscription_inactive')
await db.query('update nutris set is_suspended=false, subscription_end=null where id=$1',[target])
assert.equal((await extend({end:oldEnd})).reason,'no_expiry')
await db.query("update nutris set subscription_end=current_date-1, subscription_status='expired' where id=$1",[target])
assert.equal((await gate()).reason,'subscription_inactive')
await extend({extra:2})
assert.equal((await row()).subscription_status,'active')
assert.equal((await gate()).remaining,2)
// Monthly rollover restores the base allowance but never duplicates bonus credits.
await db.query("update nutris set subscription_type='monthly', subscription_start=current_date-interval '2 months', reports_month_reset=current_date-interval '1 month' where id=$1",[target])
assert.equal((await gate()).remaining,17)
await db.query('select increment_report_count($1)',[target])
assert.equal((await gate()).remaining,16)
assert.equal((await gate()).extra_remaining,2)
assert.equal((await row()).reports_this_month,1)
// Atomicity: failure writing the audit/credit row rolls back the date update too.
const beforeFail=await row()
await db.exec(`create function reject_extension() returns trigger language plpgsql as $$ begin raise exception 'injected failure'; end $$;
create trigger reject_extension before insert on account_extensions for each row execute function reject_extension();`)
await assert.rejects(extend({extra:5}), /injected failure/)
assert.deepEqual(await row(),beforeFail)
await db.exec('drop trigger reject_extension on account_extensions;')
// No direct grants, credit consumption, or extensions for ordinary accounts.
await db.exec(`set role authenticated; select set_config('test.uid','${other}',false);`)
assert.equal((await db.query('select * from account_extensions')).rows.length,0)
await assert.rejects(db.query('select admin_extend_account($1,$2,$3,15,0,current_date)',[admin,target,id()]),/permission denied/)
await assert.rejects(db.query('select increment_report_count($1)',[target]),/permission denied/)
await assert.rejects(db.query('update account_extensions set reports_remaining=100'),/permission denied/)
await db.query("select set_config('test.uid',$1,false)",[target])
assert.ok((await db.query('select * from account_extensions')).rows.length>0)
assert.equal((await gate()).extra_remaining,2)
await db.exec('reset role; set role service_role;')
assert.equal((await extend({extra:1})).ok,true)
await db.query('select increment_report_count($1)',[target])
await db.exec('reset role; set role anon;')
await assert.rejects(db.query('select can_generate_report($1)',[target]),/permission denied/)
await db.close()
console.log('PASS: dates, 15/30 days, zero/extra reports, retries, stale data, consumption, monthly rollover, atomic rollback, RLS and role permissions')
