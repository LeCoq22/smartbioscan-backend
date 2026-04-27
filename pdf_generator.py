"""
Generador de PDF del reporte de composición corporal Tanita.
v2 — gráfico matplotlib embebido, barras SVG con anti-colisión, sin overflows.

Uso:
    python pdf_generator.py <csv> --name "Nombre" --age 48 --sex F
                            --height 158 --doctor "Dr. X" --output reporte.pdf
"""

import argparse, math, sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from analysis_engine import PatientInfo, analyze
from csv_parser import load_csv
from svg_bars import (svg_bar, svg_ratio_bar, svg_imme_bar,
                      seg_muscle_row, seg_fat_row)
from chart_builder import build_evolution_chart_b64
from weasyprint import HTML as WeasyprintHTML


# ── helpers ──────────────────────────────────

def badge(text, style="ok"):
    cls = {"ok":"b-ok","exc":"b-exc","warn":"b-warn","est":"b-est"}.get(style,"b-ok")
    return f'<span class="badge {cls}">{text}</span>'

def delta_color(delta, invert=False):
    if delta == 0: return "#5c5b55"
    good = (delta > 0) if not invert else (delta < 0)
    return "#27ae60" if good else "#c0392b"

def delta_label(delta, unit=""):
    if delta > 0:  return f"+{delta}{unit} ↑"
    if delta < 0:  return f"{delta}{unit} ↓"
    return f"= {unit}"

def mf_bar_row(label, value, norm_l, norm_w, fill, color):
    return f"""<div class="mf-row">
  <div class="mf-lbl">{label}</div>
  <div><div class="mf-bar-wrap">
    <div class="mf-norm" style="left:{norm_l:.1f}%;width:{norm_w:.1f}%;background:{color}"></div>
    <div class="mf-fill" style="width:{min(fill,100):.2f}%;background:{color}"></div>
  </div></div>
  <div class="mf-val">{value}</div>
</div>"""


# ── CSS ──────────────────────────────────────

CSS = """
:root {
  --color-text-primary: #1a1a18;
  --color-text-secondary: #5c5b55;
  --color-text-tertiary: #9c9a92;
  --color-background-secondary: #f4f2eb;
  --color-border-tertiary: rgba(0,0,0,0.12);
  --color-border-secondary: rgba(0,0,0,0.25);
  --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:var(--font-sans); background:white; color:var(--color-text-primary); }
.r { padding:28px 32px; width:100%; max-width:1000px; margin:0 auto; }
.hdr { display:flex; justify-content:space-between; align-items:flex-start;
       border-bottom:2px solid var(--color-text-primary); padding-bottom:10px; margin-bottom:14px; }
.hdr-logo { font-size:20px; font-weight:500; letter-spacing:1px; }
.id-bar { display:grid; grid-template-columns:repeat(5,1fr);
          border:0.5px solid var(--color-border-secondary); margin-bottom:14px; }
.id-cell { padding:5px 10px; border-right:0.5px solid var(--color-border-secondary); }
.id-cell:last-child { border-right:none; }
.id-key { font-size:10px; color:var(--color-text-secondary); }
.id-val { font-size:13px; font-weight:500; margin-top:1px; }
.sec { font-size:12px; font-weight:500; border-bottom:1.5px solid var(--color-text-primary);
       padding-bottom:3px; margin:14px 0 8px; }
.badge { display:inline-block; font-size:9px; padding:1px 5px; border-radius:3px;
         font-weight:500; margin-left:3px; }
.b-ok  { background:#e8f5e9; color:#2e7d32; }
.b-warn{ background:#fff8e1; color:#f57f17; }
.b-exc { background:#e8f5e9; color:#2e7d32; }
.b-est { background:#fff8e1; color:#f57f17; font-size:8px; }
.params-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr));
               gap:8px; margin-bottom:14px; }
.param-card { background:var(--color-background-secondary); border-radius:8px;
              padding:8px 10px; border:0.5px solid var(--color-border-tertiary); }
.param-label { font-size:10px; color:var(--color-text-secondary); margin-bottom:2px; }
.param-val   { font-size:15px; font-weight:500; }
.param-sub   { font-size:9px; color:var(--color-text-tertiary); margin-top:2px; }
.comp-block-wrap { margin-bottom:10px; }
.comp-block-title { font-size:11px; font-weight:500; margin-bottom:4px;
                    display:flex; align-items:center; gap:6px; }
.comp-block-title::before { content:'●'; font-size:8px; color:#c0392b; }
.cblock { background:var(--color-background-secondary);
          border:0.5px solid var(--color-border-tertiary); border-radius:4px; padding:8px 10px; }
.agua-block { background:var(--color-background-secondary);
              border:0.5px solid var(--color-border-tertiary);
              border-left:3px solid #2980b9; border-radius:4px; padding:8px 10px; }
.bar-header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
.bar-val  { font-size:13px; font-weight:500; }
.bar-rng  { font-size:9px; color:var(--color-text-tertiary); }
.bar-lbl  { font-size:10px; color:var(--color-text-secondary); margin-bottom:3px; }
.bar-div  { height:0.5px; background:var(--color-border-tertiary); margin:8px 0; }
.bar-note { font-size:9px; color:var(--color-text-tertiary); margin-top:6px; line-height:1.5; }
.svg-wrap { width:100%; overflow:hidden; }
.comp-2col { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.agua-3grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:10px; }
.agua-cell { text-align:center; padding:6px; background:white; border-radius:3px;
             border:0.5px solid var(--color-border-tertiary); }
.agua-cell-lbl { font-size:9px; color:var(--color-text-secondary); margin-bottom:2px; }
.agua-cell-val { font-size:18px; font-weight:500; line-height:1.1; }
.agua-cell-unit { font-size:10px; color:var(--color-text-secondary); }
.agua-cell-sub  { font-size:8px; color:var(--color-text-tertiary); margin-top:2px; }
.agua-prop-bar  { display:flex; height:18px; border-radius:3px; overflow:hidden; margin-bottom:8px; }
.agua-prop-seg  { display:flex; align-items:center; justify-content:center;
                  font-size:9px; font-weight:500; color:white; }
.error-note { font-size:9px; color:var(--color-text-tertiary); padding:5px 8px;
              background:var(--color-background-secondary);
              border:0.5px solid var(--color-border-tertiary);
              border-radius:3px; margin-top:6px; line-height:1.5; }
.error-highlight { color:#e67e22; font-weight:500; }
.mme-block { background:var(--color-background-secondary);
             border:0.5px solid var(--color-border-tertiary);
             border-left:3px solid #2980b9; border-radius:4px;
             padding:8px 10px; margin:6px 0; }
.mme-3grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:8px; }
.mme-cell { text-align:center; padding:6px; background:white; border-radius:3px;
            border:0.5px solid var(--color-border-tertiary); }
.mme-cell-lbl  { font-size:9px; color:var(--color-text-secondary); margin-bottom:2px; }
.mme-cell-val  { font-size:16px; font-weight:500; }
.mme-cell-unit { font-size:10px; color:var(--color-text-secondary); }
.mme-cell-sub  { font-size:8px; color:var(--color-text-tertiary); margin-top:2px; }
.seg-header { display:grid; grid-template-columns:80px 1fr 52px 60px 100px;
              gap:3px; font-size:9px; color:var(--color-text-tertiary);
              margin-bottom:3px; padding-bottom:2px;
              border-bottom:0.5px solid var(--color-border-tertiary); }
.seg-row    { display:grid; grid-template-columns:80px 1fr 52px 60px 100px;
              gap:3px; align-items:center; margin-bottom:5px; }
.seg-name   { font-size:11px; font-weight:500; }
.seg-bar-outer { position:relative; height:16px;
                 background:var(--color-background-secondary);
                 border:0.5px solid var(--color-border-tertiary);
                 overflow:hidden; }
.seg-bar  { position:absolute; top:2px; bottom:2px; left:0; border-radius:1px; }
.seg-pct  { font-size:11px; font-weight:500; text-align:right; }
.seg-cat  { font-size:10px; text-align:center; }
.seg-det  { font-size:10px; color:var(--color-text-secondary); }
.mf-row  { display:grid; grid-template-columns:110px 1fr 52px;
           align-items:center; gap:7px; margin-bottom:5px; }
.mf-lbl  { font-size:10px; color:var(--color-text-secondary); }
.mf-bar-wrap { position:relative; height:14px;
               background:var(--color-border-tertiary);
               border-radius:1px; overflow:hidden; }
.mf-norm { position:absolute; top:0; bottom:0; border-radius:1px; opacity:0.2; }
.mf-fill { position:absolute; top:2px; bottom:2px; left:0; border-radius:1px; }
.mf-val  { font-size:12px; font-weight:500; text-align:right; }
.visc-panel { background:var(--color-background-secondary);
              border:0.5px solid var(--color-border-tertiary);
              border-radius:4px; padding:10px; }
.visc-note { font-size:9px; color:var(--color-text-tertiary); line-height:1.5;
             border-top:0.5px solid var(--color-border-tertiary);
             padding-top:6px; margin-top:6px; }
.grasa-with-visc { display:grid; grid-template-columns:1fr 190px; gap:16px; align-items:start; }
.balance-piernas  { display:grid; grid-template-columns:230px 1fr; gap:20px; align-items:start; }
.bar-legend { display:flex; gap:10px; margin-bottom:7px; flex-wrap:wrap; }
.bl-i { display:flex; align-items:center; gap:4px; font-size:10px; color:var(--color-text-secondary); }
.bl-b { width:9px; height:9px; border-radius:2px; }
.scenario { border:0.5px solid var(--color-border-tertiary); border-radius:4px;
            padding:7px 9px; margin-bottom:7px; font-size:11px; }
.sc-min { border-left:3px solid #27ae60; }
.sc-opt { border-left:3px solid #2980b9; }
.sc-header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:5px; }
.sc-title  { font-size:10px; font-weight:500; }
.sc-badge  { font-size:9px; padding:1px 5px; border-radius:3px; font-weight:500; }
.sb-min { background:#e8f5e9; color:#2e7d32; }
.sb-opt { background:#e3f2fd; color:#1565c0; }
.sc-row { display:flex; justify-content:space-between; padding:2px 0;
          border-bottom:0.5px solid var(--color-border-tertiary); }
.sc-row:last-child { border-bottom:none; }
.sc-key { color:var(--color-text-secondary); font-size:10px; }
.sc-val { font-weight:500; font-size:10px; }
.ch-up  { color:#27ae60; font-size:9px; margin-left:3px; }
.trend-ok { font-size:10px; color:#27ae60; margin-top:5px; line-height:1.5;
            padding:5px 7px; border-left:2px solid #27ae60;
            background:var(--color-background-secondary); }
.ref-section { margin-top:16px; border-top:1.5px solid var(--color-text-primary); padding-top:10px; }
.ref-group { margin-bottom:10px; }
.ref-group-title { font-size:10px; font-weight:500; color:var(--color-text-secondary);
                   margin-bottom:4px; padding-bottom:2px;
                   border-bottom:0.5px solid var(--color-border-tertiary); }
.ref-item { display:grid; grid-template-columns:22px 1fr; gap:4px;
            font-size:9.5px; color:var(--color-text-secondary);
            margin-bottom:3px; line-height:1.5; }
.ref-num  { font-weight:500; color:var(--color-text-primary); text-align:right; }
.ref-uses { font-size:9px; color:var(--color-text-tertiary); display:block; margin-top:1px; }
"""


# ── Generador HTML ────────────────────────────

def generate_html(result: dict, doctor_name: str = "") -> str:
    p  = result['patient']
    m  = result['measurement']
    k  = result['key_params']
    act    = result['act']
    prot   = result['protein']
    bone   = result['bone']
    fat    = result['fat']
    muscle = result['muscle']
    bal    = result['balance']
    weight = result['weight_control']
    evol   = result['evolution']

    # Fecha
    try:
        dt = datetime.strptime(m.date[:16], "%Y-%m-%d %H:%M")
        fecha = dt.strftime("%d/%m/%Y · %H:%M")
    except:
        fecha = m.date[:16]

    sex_lbl = "Femenino" if p.sex == 'F' else "Masculino"
    dec = p.age // 10 * 10
    doc_html = (f'<div style="font-size:10px;color:var(--color-text-secondary);margin-top:2px">'
                f'Médico tratante: {doctor_name}</div>') if doctor_name else ""

    # ── Badges parámetros clave ──
    visc_b  = badge("Saludable","exc") if m.visceral_fat<=12 else badge("Levemente alto","warn")
    meta_d  = k['metabolic_age']['diff']
    meta_b  = badge(f"{'−' if meta_d>0 else '+'}{abs(meta_d)}", "ok" if meta_d>0 else "warn")
    fat_s   = "exc" if k['fat_cat'] in ('Atlético','Fitness') else ("ok" if k['fat_cat']=='Normal' else "warn")
    act_b   = badge(k['act_pct_cat'], "ok" if k['act_pct_cat']=='Normal' else "warn")
    imme_b  = badge(k['imme_cat'], "ok" if k['imme_cat']=='Sin sarcopenia' else "warn")
    leg_b   = badge(k['leg_score_cat'], "exc" if '↑' in k['leg_score_cat'] else "ok")
    bal_b   = badge("Normal","ok") if bal['arm_cat']=='Normal' else badge("Asimétrico","warn")

    # ── ACT ──
    act_kg_range = act['act_kg_range']
    act_kg_bar = svg_bar(
        act['act_kg'], act_kg_range[0], act_kg_range[1],
        scale_min=max(0, act_kg_range[0]-12), scale_max=act_kg_range[1]+22,
        fill_color="#e67e22" if act['act_kg_cat']!='Normal' else "#2980b9"
    )
    act_pct_range = act['act_pct_range']
    act_pct_bar = svg_bar(
        act['act_pct'], act_pct_range[0], act_pct_range[1],
        scale_min=max(0, act_pct_range[0]-10), scale_max=act_pct_range[1]+12,
        fill_color="#2980b9" if act['act_pct_cat']=='Normal' else "#e67e22",
        norm_color="rgba(41,128,185,0.22)", label_unit="%"
    )
    ratio_bar = svg_ratio_bar(act['ratio'])
    aec_pct, aic_pct = act['aec_pct'], act['aic_pct']

    # ── Proteína ──
    p_kg_r = prot['ref_tanita_kg']
    prot_kg_bar = svg_bar(
        prot['protein_kg'], p_kg_r[0], p_kg_r[1],
        scale_min=0, scale_max=20,
        fill_color="#27ae60" if prot['pct_cat']=='Normal' else "#e67e22"
    )
    p_pct_r = prot['ref_pct']
    prot_pct_bar = svg_bar(
        prot['protein_pct'], p_pct_r[0], p_pct_r[1],
        scale_min=max(0,p_pct_r[0]-7), scale_max=p_pct_r[1]+8,
        fill_color="#27ae60" if prot['pct_cat']=='Normal' else "#e67e22",
        norm_color="rgba(41,128,185,0.22)", label_unit="%"
    )

    # ── Hueso ──
    b_kg_r = bone['ref_kg']
    bone_kg_bar = svg_bar(
        bone['bone_kg'], b_kg_r[0], b_kg_r[1],
        scale_min=0, scale_max=6,
        fill_color="#e67e22" if bone['kg_cat']!='Normal' else "#27ae60"
    )
    b_pct_r = bone['ref_pct']
    bone_pct_bar = svg_bar(
        bone['bone_pct'], b_pct_r[0], b_pct_r[1],
        scale_min=1, scale_max=7,
        fill_color="#27ae60" if bone['pct_cat']=='Normal' else "#e67e22",
        norm_color="rgba(41,128,185,0.22)", label_unit="%"
    )

    # ── Grasa ──
    fn = fat['fat_ref_normal']
    fat_c = "#27ae60" if fat['global_cat'] in ('Atlético','Fitness') else "#e67e22"
    fat_kg_bar = svg_bar(
        fat['fat_kg'], fn[0]/100*m.weight_kg, fn[1]/100*m.weight_kg,
        scale_min=0, scale_max=50,
        fill_color=fat_c,
        norm_color="rgba(231,76,60,0.20)"
    )
    fat_pct_bar = svg_bar(
        fat['fat_pct'], fn[0], fn[1],
        scale_min=max(0,fn[0]-9), scale_max=fn[1]+19,
        fill_color=fat_c,
        norm_color="rgba(231,76,60,0.20)", label_unit="%"
    )

    # ── Músculo-Grasa barras horizontales ──
    mf_p = mf_bar_row("Peso (kg)",          m.weight_kg,        40,15, m.weight_kg,        "#2c3e50")
    mf_m = mf_bar_row("Masa muscular (kg)", m.muscle_mass_kg,   30,15, m.muscle_mass_kg,   "#2980b9")
    fat_fill_c = "#27ae60" if fat['global_cat'] in ('Atlético','Fitness') else "#e67e22"
    mf_g = mf_bar_row("Masa grasa (kg)",    fat['fat_kg'],      11,13, fat['fat_kg'],      fat_fill_c)

    # ── IMME ──
    imme_bar = svg_imme_bar(
        muscle['imme'], muscle['imme_sarcopenia_threshold'],
        muscle['imme_normal_threshold'], p.name.split()[0]
    )

    # ── Segmental músculo ──
    mq_range = muscle['leg_score_normal']
    seg_mus = ""
    for lbl, key, ideal in [
        ("Tronco",      "trunk",     muscle['ideal_trunk_kg']),
        ("Brazo izq.",  "left_arm",  muscle['ideal_arm_kg']),
        ("Brazo der.",  "right_arm", muscle['ideal_arm_kg']),
        ("Pierna izq.", "left_leg",  muscle['ideal_leg_kg']),
        ("Pierna der.", "right_leg", muscle['ideal_leg_kg']),
    ]:
        seg = muscle['segmental'][key]
        seg_mus += seg_muscle_row(lbl, seg['kg'], ideal, seg['quality'], mq_range)

    # ── Segmental grasa ──
    seg_fat = ""
    fat_refs = {'trunk':(20,28),'left_arm':(23,33),'right_arm':(23,33),'left_leg':(26,38),'right_leg':(26,38)}
    fat_ideals = {'trunk':24,'left_arm':28,'right_arm':28,'left_leg':32,'right_leg':32}
    for lbl, key in [("Tronco","trunk"),("Brazo izq.","left_arm"),("Brazo der.","right_arm"),
                     ("Pierna izq.","left_leg"),("Pierna der.","right_leg")]:
        seg = fat['segmental'][key]
        r = fat_refs[key]
        seg_fat += seg_fat_row(lbl, seg['pct'], r[0], r[1], fat_ideals[key])

    # ── Evolución ──
    ew = evol.get('delta_weight',0); em = evol.get('delta_muscle',0)
    ef = evol.get('delta_fat_pct',0); et = evol.get('delta_tmb',0)
    ew_c = delta_color(ew, invert=True); em_c = delta_color(em)
    ef_c = delta_color(ef, invert=True); et_c = delta_color(et)
    date_range = f"{evol.get('date_first','')[:7].replace('-',' ')} → {evol.get('date_last','')[:7].replace('-',' ')}"
    trend_note = evol.get('trend_note','')

    # ── Gráfico matplotlib ──
    chart_img = build_evolution_chart_b64(evol)
    chart_html = (f'<img src="{chart_img}" style="width:100%;height:auto;border-radius:4px" />'
                  if chart_img else '<div style="font-size:9px;color:#9c9a92">Sin datos suficientes para el gráfico.</div>')

    # ── Control de peso ──
    delta_proj = round(weight['projected_weight'] - m.weight_kg, 1)
    delta_proj_str = f"+{delta_proj}" if delta_proj > 0 else str(delta_proj)
    proj_formula = (f"({weight['mlg_kg']}+{weight['target_muscle_gain']:.0f})"
                    f"÷(1−{m.body_fat_pct/100:.3f})={weight['projected_weight']} kg")

    # ── HTML ──────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Reporte {p.name}</title>
<style>{CSS}</style>
</head>
<body>
<div class="r">

<!-- HEADER -->
<div class="hdr">
  <div>
    <div class="hdr-logo">TANITA</div>
    <div style="font-size:10px;color:var(--color-text-secondary);margin-top:2px">Resultados de Composición Corporal</div>
  </div>
  <div style="text-align:right">
    <div style="font-size:15px;font-weight:500">{p.name}</div>
    <div style="font-size:11px;color:var(--color-text-secondary);margin-top:2px">{fecha}</div>
    {doc_html}
  </div>
</div>

<!-- DATOS BÁSICOS -->
<div class="id-bar">
  <div class="id-cell"><div class="id-key">Altura</div><div class="id-val">{int(p.height_cm)} cm</div></div>
  <div class="id-cell"><div class="id-key">Edad</div><div class="id-val">{p.age} años</div></div>
  <div class="id-cell"><div class="id-key">Sexo</div><div class="id-val">{sex_lbl}</div></div>
  <div class="id-cell"><div class="id-key">Peso</div><div class="id-val">{m.weight_kg} kg</div></div>
  <div class="id-cell"><div class="id-key">IMC</div><div class="id-val">{m.bmi}</div></div>
</div>

<!-- PARÁMETROS CLAVE -->
<div class="sec">Parámetros clave</div>
<div class="params-grid">
  <div class="param-card">
    <div class="param-label">TMB</div>
    <div class="param-val">{int(m.bmr_kcal)} <span style="font-size:11px;font-weight:400;color:var(--color-text-secondary)">kcal</span></div>
    <div class="param-sub">Tasa metabólica basal</div>
  </div>
  <div class="param-card">
    <div class="param-label">Grasa visceral</div>
    <div class="param-val">{int(m.visceral_fat)} {visc_b}</div>
    <div class="param-sub">Rango saludable: 1–12</div>
  </div>
  <div class="param-card">
    <div class="param-label">Edad metabólica</div>
    <div class="param-val">{m.metabolic_age} años {meta_b}</div>
    <div class="param-sub">{k['metabolic_age']['label']}</div>
  </div>
  <div class="param-card">
    <div class="param-label">% grasa global</div>
    <div class="param-val">{m.body_fat_pct}% {badge(k['fat_cat'],fat_s)}</div>
    <div class="param-sub">Normal {dec}–{dec+9}: {fn[0]}–{fn[1]}%</div>
  </div>
  <div class="param-card">
    <div class="param-label">ACT</div>
    <div class="param-val">{act['act_kg']} <span style="font-size:11px;font-weight:400;color:var(--color-text-secondary)">L</span></div>
    <div class="param-sub">{act['act_pct']}% {act_b}</div>
  </div>
  <div class="param-card">
    <div class="param-label">MME estimada {badge('±6%','est')}</div>
    <div class="param-val">{muscle['mme_kg']} <span style="font-size:11px;font-weight:400;color:var(--color-text-secondary)">kg</span></div>
    <div class="param-sub">IMME {muscle['imme']} — {imme_b}</div>
  </div>
  <div class="param-card">
    <div class="param-label">Balance muscular</div>
    <div class="param-val">Brazos Δ{bal['arm_diff_pct']}% {bal_b}</div>
    <div class="param-sub">Piernas Δ{bal['leg_diff_pct']}% — {bal['leg_label']}</div>
  </div>
  <div class="param-card">
    <div class="param-label">Punt. piernas</div>
    <div class="param-val">{int(muscle['leg_score'])} {leg_b}</div>
    <div class="param-sub">Normal {'mujer' if p.sex=='F' else 'hombre'} {dec}–{dec+9}: {muscle['leg_score_normal'][0]}–{muscle['leg_score_normal'][1]}</div>
  </div>
</div>

<!-- ANÁLISIS COMPOSICIÓN -->
<div class="sec">Análisis de Composición Corporal</div>

<!-- ACT -->
<div class="comp-block-wrap">
  <div class="comp-block-title">Agua corporal total <span style="color:var(--color-text-tertiary);font-size:10px;font-weight:400">[1,2,14,15]</span></div>
  <div class="agua-block">
    <div class="comp-2col">
      <div>
        <div class="bar-lbl">Cantidad total (ACT)</div>
        <div class="bar-header">
          <div><span class="bar-val">{act['act_kg']} kg</span>
          {badge('↓ leve en kg','warn') if act['act_kg_cat']!='Normal' else badge('Normal','ok')}</div>
          <span class="bar-rng">Normal: {act_kg_range[0]}–{act_kg_range[1]} kg</span>
        </div>
        <div class="svg-wrap">{act_kg_bar}</div>
      </div>
      <div>
        <div class="bar-lbl">Porcentaje corporal</div>
        <div class="bar-header">
          <div><span class="bar-val">{act['act_pct']} %</span>
          {badge(act['act_pct_cat'],'ok' if act['act_pct_cat']=='Normal' else 'warn')}</div>
          <span class="bar-rng">Normal {'mujer' if p.sex=='F' else 'hombre'}: {act_pct_range[0]}–{act_pct_range[1]}%</span>
        </div>
        <div class="svg-wrap">{act_pct_bar}</div>
        <div class="bar-note">{act['note']}</div>
      </div>
    </div>
    <div class="bar-div"></div>
    <div style="font-size:10px;font-weight:500;margin-bottom:6px">Compartimentos estimados
      <span style="font-size:9px;font-weight:400;color:var(--color-text-tertiary)">Estimativo · ±10–15%</span>
    </div>
    <div class="agua-3grid">
      <div class="agua-cell">
        <div class="agua-cell-lbl">Agua extracelular (AEC)</div>
        <div><span class="agua-cell-val">{act['aec_l']}</span><span class="agua-cell-unit"> L</span></div>
        <div class="agua-cell-sub">proporcional al peso <strong>Normal</strong></div>
      </div>
      <div class="agua-cell">
        <div class="agua-cell-lbl">Agua intracelular (AIC)</div>
        <div><span class="agua-cell-val">{act['aic_l']}</span><span class="agua-cell-unit"> L</span></div>
        <div class="agua-cell-sub">proporcional al peso <strong>Normal</strong></div>
      </div>
      <div class="agua-cell">
        <div class="agua-cell-lbl">Ratio AEC/AIC</div>
        <div><span class="agua-cell-val">{act['ratio']}</span></div>
        <div class="agua-cell-sub">normal: {act['ratio_normal'][0]}–{act['ratio_normal'][1]} <strong>{act['ratio_cat']}</strong></div>
      </div>
    </div>
    <div class="agua-prop-bar">
      <div class="agua-prop-seg" style="width:{aec_pct}%;background:#2980b9">AEC {act['aec_l']} L ({aec_pct}%)</div>
      <div class="agua-prop-seg" style="width:{aic_pct}%;background:#1a5276">AIC {act['aic_l']} L ({aic_pct}%)</div>
    </div>
    <div class="svg-wrap">{ratio_bar}</div>
    <div class="error-note">△ <strong>Estimativo.</strong> ke={act['ke']} (De Lorenzo 1997). Error <span class="error-highlight">±10–15%</span>.</div>
  </div>
</div>

<!-- PROTEÍNA Y HUESO -->
<div class="comp-2col" style="margin-bottom:10px">
  <div class="comp-block-wrap" style="margin-bottom:0">
    <div class="comp-block-title">Proteína <span style="color:#9c9a92;font-size:10px;font-weight:400">[3,4]</span></div>
    <div class="cblock">
      <div class="bar-lbl">Cantidad</div>
      <div class="bar-header">
        <div><span class="bar-val">{prot['protein_kg']} kg</span> {badge('Normal','ok') if prot['pct_cat']=='Normal' else badge('Bajo','warn')}</div>
        <span class="bar-rng">Ref. Tanita: {p_kg_r[0]}–{p_kg_r[1]} kg*</span>
      </div>
      <div class="svg-wrap">{prot_kg_bar}</div>
      <div class="bar-div"></div>
      <div class="bar-lbl">Porcentaje del peso</div>
      <div class="bar-header">
        <div><span class="bar-val">{prot['protein_pct']} %</span> {badge(prot['pct_cat'],'ok')}</div>
        <span class="bar-rng">Normal: {p_pct_r[0]}–{p_pct_r[1]}%</span>
      </div>
      <div class="svg-wrap">{prot_pct_bar}</div>
      {'<div class="bar-note">'+prot["note"]+'</div>' if prot['note'] else ''}
    </div>
  </div>
  <div class="comp-block-wrap" style="margin-bottom:0">
    <div class="comp-block-title">Masa ósea <span style="color:#9c9a92;font-size:10px;font-weight:400">[5,6]</span></div>
    <div class="cblock">
      <div class="bar-lbl">Cantidad</div>
      <div class="bar-header">
        <div><span class="bar-val">{bone['bone_kg']} kg</span> {badge(bone['kg_cat'],'warn' if bone['kg_cat']!='Normal' else 'ok')}</div>
        <span class="bar-rng">Normal {'mujer' if p.sex=='F' else 'hombre'} {dec}–{dec+9}: {b_kg_r[0]}–{b_kg_r[1]} kg</span>
      </div>
      <div class="svg-wrap">{bone_kg_bar}</div>
      <div class="bar-div"></div>
      <div class="bar-lbl">Porcentaje del peso</div>
      <div class="bar-header">
        <div><span class="bar-val">{bone['bone_pct']} %</span> {badge(bone['pct_cat'],'ok')}</div>
        <span class="bar-rng">Normal: {b_pct_r[0]}–{b_pct_r[1]}%</span>
      </div>
      <div class="svg-wrap">{bone_pct_bar}</div>
      {'<div class="bar-note">'+bone['note']+'</div>' if bone['note'] else ''}
    </div>
  </div>
</div>

<!-- MASA GRASA -->
<div class="comp-block-wrap">
  <div class="comp-block-title">Masa grasa <span style="color:#9c9a92;font-size:10px;font-weight:400">[7,8]</span></div>
  <div class="grasa-with-visc">
    <div class="cblock">
      <div class="comp-2col">
        <div>
          <div class="bar-lbl">Cantidad</div>
          <div class="bar-header">
            <div><span class="bar-val">{fat['fat_kg']} kg</span>{badge('↓ excelente','exc') if fat['global_cat'] in ('Atlético','Fitness') else badge('Normal','ok')}</div>
            <span class="bar-rng">Normal {dec}–{dec+9}: {fn[0]}–{fn[1]}%</span>
          </div>
          <div class="svg-wrap">{fat_kg_bar}</div>
        </div>
        <div>
          <div class="bar-lbl">Porcentaje del peso</div>
          <div class="bar-header">
            <div><span class="bar-val">{fat['fat_pct']} %</span>{badge('↓ excelente','exc') if fat['global_cat'] in ('Atlético','Fitness') else badge('Normal','ok')}</div>
            <span class="bar-rng">Normal {dec}–{dec+9}: {fn[0]}–{fn[1]}%</span>
          </div>
          <div class="svg-wrap">{fat_pct_bar}</div>
        </div>
      </div>
      <div class="bar-note">{fat['note']}</div>
    </div>
    <div class="visc-panel">
      <div style="font-size:10px;font-weight:500;margin-bottom:6px">GRASA VISCERAL</div>
      <div style="font-size:32px;font-weight:300;line-height:1">{int(m.visceral_fat)}</div>
      <div style="font-size:10px;margin-bottom:8px">nivel actual</div>
      <div style="font-size:10px;color:#27ae60;font-weight:500;margin-bottom:8px">{'Saludable' if m.visceral_fat<=12 else 'Levemente alto' if m.visceral_fat<=19 else 'Alto'}</div>
      <div style="background:linear-gradient(to right,#27ae60 0%,#27ae60 20%,#e67e22 20%,#e67e22 38%,#e74c3c 38%,#e74c3c 100%);height:12px;border-radius:2px;position:relative;margin-bottom:4px">
        <div style="position:absolute;top:-4px;left:{min(int(m.visceral_fat)/59*100,99):.1f}%;width:2px;height:20px;background:#1a1a18;border-radius:1px"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:8px;color:var(--color-text-tertiary);margin-bottom:6px"><span>1</span><span>12</span><span>19</span><span>59</span></div>
      <div style="font-size:9px;margin-bottom:6px;line-height:1.6">
        <div><span style="color:#27ae60">1–12</span> Saludable ★</div>
        <div><span style="color:#e67e22">13–19</span> Levemente alto</div>
        <div><span style="color:#e74c3c">20–59</span> Alto</div>
      </div>
      <div class="visc-note">Fuente: Tanita Europe. Niveles elevados se asocian a riesgo cardiovascular y diabetes tipo 2.</div>
    </div>
  </div>
</div>

<!-- PESO TOTAL -->
<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 8px;
            background:var(--color-background-secondary);border-radius:3px;
            border:0.5px solid rgba(0,0,0,0.25);font-size:11px;margin-bottom:4px">
  <span style="color:var(--color-text-secondary)">Peso total</span>
  <span style="font-weight:500">{m.weight_kg} kg {badge('Normal','ok')}</span>
</div>

<!-- ANÁLISIS MÚSCULO-GRASA -->
<div class="sec">Análisis Músculo–Grasa</div>
<div style="font-size:9px;color:#9c9a92;margin-bottom:8px">Escala unificada 0–100 kg · zona sombreada = rango normal</div>
{mf_p}{mf_m}{mf_g}
<div style="display:grid;grid-template-columns:110px 1fr 52px;gap:7px;margin-bottom:8px">
  <div></div>
  <div style="display:flex;justify-content:space-between;font-size:8px;color:#9c9a92">
    {''.join(f'<span>{i}</span>' for i in range(0,101,10))}
  </div>
  <div></div>
</div>

<!-- MME/IMME -->
<div class="mme-block">
  <div style="font-size:10px;font-weight:500;margin-bottom:7px;display:flex;justify-content:space-between;align-items:baseline">
    <span>Masa Muscular Esquelética (MME) — factor de conversión [12,13]</span>
    {badge('Estimativo · ±6%','est')}
  </div>
  <div class="mme-3grid">
    <div class="mme-cell">
      <div class="mme-cell-lbl">Masa musc. Tanita</div>
      <div><span class="mme-cell-val">{m.muscle_mass_kg}</span><span class="mme-cell-unit"> kg</span></div>
      <div class="mme-cell-sub">Incluye mus. liso + cardíaco</div>
    </div>
    <div class="mme-cell">
      <div class="mme-cell-lbl">MME estimada ÷{muscle['conversion_factor']}</div>
      <div><span class="mme-cell-val">{muscle['mme_kg']}</span><span class="mme-cell-unit"> kg</span></div>
      <div class="mme-cell-sub">Rango: {muscle['mme_range'][0]}–{muscle['mme_range'][1]} kg</div>
    </div>
    <div class="mme-cell">
      <div class="mme-cell-lbl">IMME (MME÷alt²)</div>
      <div><span class="mme-cell-val">{muscle['imme']}</span><span class="mme-cell-unit"> kg/m²</span></div>
      <div class="mme-cell-sub">{badge(muscle['imme_cat'],'ok' if muscle['imme_cat']=='Sin sarcopenia' else 'warn')}</div>
    </div>
  </div>
  <div style="font-size:9px;color:var(--color-text-secondary);margin-bottom:3px">IMME — escala 0–20 kg/m² · umbrales EWGSOP2 2019</div>
  {imme_bar}
  <div class="error-note">⚠ <strong>Estimativo.</strong> Factor ÷{muscle['conversion_factor']} (rango {muscle['conversion_factor_range'][0]}–{muscle['conversion_factor_range'][1]}). Error <span class="error-highlight">±5–6%</span>. Rango probable: {muscle['mme_range'][0]}–{muscle['mme_range'][1]} kg.</div>
</div>

<!-- SEGMENTAL MÚSCULO -->
<div class="sec">Masa Muscular Segmental — Cantidad y Calidad</div>
<div class="bar-legend">
  <div class="bl-i"><div class="bl-b" style="background:#2980b9"></div>Cantidad (% del ideal {int(p.height_cm)} cm)</div>
  <div class="bl-i"><div class="bl-b" style="background:#8e44ad"></div>Calidad (score, Normal {'mujer' if p.sex=='F' else 'hombre'} {dec}–{dec+9}: {muscle['leg_score_normal'][0]}–{muscle['leg_score_normal'][1]})</div>
  <div class="bl-i"><div class="bl-b" style="background:rgba(41,128,185,0.15)"></div>Zona normal 80–120%</div>
</div>
<div class="seg-header"><div>Segmento</div><div style="padding-left:4px">60%–80%–100%–120%–140%</div><div>%</div><div>Cat.</div><div>Valor real</div></div>
{seg_mus}

<!-- SEGMENTAL GRASA -->
<div class="sec">Grasa Segmental — Nivel relativo y % real</div>
<div class="bar-legend">
  <div class="bl-i"><div class="bl-b" style="background:#e67e22;opacity:0.85"></div>Nivel relativo al ideal (zona sombreada = normal 80–120%)</div>
  <div class="bl-i"><div class="bl-b" style="background:#c0392b;opacity:0.8"></div>% grasa real del segmento</div>
</div>
<div class="seg-header"><div>Segmento</div><div style="padding-left:4px">60%–80%–100%–120%–140%</div><div>Niv. Cat.</div><div></div><div>% real · ref.</div></div>
{seg_fat}

<!-- BALANCE Y PIERNAS -->
<div class="sec">Balance muscular y puntuación de piernas</div>
<div class="balance-piernas">
  <div>
    <div style="font-size:10px;font-weight:500;margin-bottom:8px">Balance de masa muscular</div>
    <div style="display:flex;gap:12px;align-items:flex-end;margin-bottom:8px">
      <div style="text-align:center">
        <div style="font-size:11px;font-weight:500">{bal['arm_left_kg']}</div>
        <div style="width:24px;height:{int(bal['arm_left_kg']*10)}px;background:#2980b9;margin:4px auto;border-radius:2px 2px 0 0;min-height:20px"></div>
        <div style="font-size:9px;color:var(--color-text-tertiary)">Izq.</div>
      </div>
      <div style="text-align:center;padding-bottom:16px">
        <div style="font-size:9px;color:var(--color-text-secondary)">Δ{bal['arm_diff_pct']}%</div>
        <div style="font-size:9px">{bal['arm_cat']}</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:11px;font-weight:500">{bal['arm_right_kg']}</div>
        <div style="width:24px;height:{int(bal['arm_right_kg']*10)}px;background:#2980b9;margin:4px auto;border-radius:2px 2px 0 0;min-height:20px"></div>
        <div style="font-size:9px;color:var(--color-text-tertiary)">Der.</div>
      </div>
      <div style="width:2px;height:60px;background:rgba(0,0,0,0.1);align-self:flex-end"></div>
      <div style="text-align:center">
        <div style="font-size:11px;font-weight:500">{bal['leg_left_kg']}</div>
        <div style="width:24px;height:{int(bal['leg_left_kg']*8)}px;background:#2980b9;margin:4px auto;border-radius:2px 2px 0 0;min-height:30px"></div>
        <div style="font-size:9px;color:var(--color-text-tertiary)">Izq.</div>
      </div>
      <div style="text-align:center;padding-bottom:24px">
        <div style="font-size:9px;color:var(--color-text-secondary)">Δ{bal['leg_diff_pct']}%</div>
        <div style="font-size:9px">{bal['leg_cat']}</div>
      </div>
      <div style="text-align:center">
        <div style="font-size:11px;font-weight:500">{bal['leg_right_kg']}</div>
        <div style="width:24px;height:{int(bal['leg_right_kg']*8)}px;background:#2980b9;margin:4px auto;border-radius:2px 2px 0 0;min-height:30px"></div>
        <div style="font-size:9px;color:var(--color-text-tertiary)">Der.</div>
      </div>
    </div>
    <div style="font-size:9px;color:var(--color-text-tertiary)">
      Brazos Δ{bal['arm_diff_pct']}% · Piernas Δ{bal['leg_diff_pct']}%<br>
      Ambos dentro del límite normal (&lt;10%)
    </div>
  </div>
  <div>
    <div style="font-size:10px;font-weight:500;margin-bottom:6px">Puntuación muscular de piernas</div>
    <div style="background:var(--color-background-secondary);border:0.5px solid var(--color-border-tertiary);border-radius:4px;padding:10px;text-align:center">
      <div style="font-size:32px;font-weight:300;line-height:1">{int(muscle['leg_score'])}</div>
      <div style="font-size:10px;color:var(--color-text-secondary)">score piernas</div>
      <div style="font-size:10px;margin-top:6px">{badge(muscle['leg_score_cat'],'exc' if '↑' in muscle['leg_score_cat'] else 'ok')}</div>
      <div style="font-size:9px;color:var(--color-text-tertiary);margin-top:4px">
        Normal {'mujer' if p.sex=='F' else 'hombre'} {dec}–{dec+9}: {muscle['leg_score_normal'][0]}–{muscle['leg_score_normal'][1]}
      </div>
    </div>
  </div>
</div>

<!-- CONTROL DE PESO Y EVOLUCIÓN -->
<div class="sec">Control de peso y evolución</div>
<div class="comp-2col">
  <div>
    <div style="font-size:10px;font-weight:500;margin-bottom:6px">Control de peso por composición</div>
    <div style="font-size:9px;color:var(--color-text-tertiary);margin-bottom:4px">MLG real = {weight['mlg_kg']} kg. Sin uso de IMC.</div>
    <div style="background:var(--color-background-secondary);border-radius:3px;padding:5px 7px;
                margin-bottom:7px;border:0.5px solid var(--color-border-tertiary);font-size:11px">
      <div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:0.5px solid rgba(0,0,0,0.12)"><span style="color:#5c5b55">Peso</span><span style="font-weight:500">{m.weight_kg} kg</span></div>
      <div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:0.5px solid rgba(0,0,0,0.12)"><span style="color:#5c5b55">MLG</span><span style="font-weight:500">{weight['mlg_kg']} kg</span></div>
      <div style="display:flex;justify-content:space-between;padding:2px 0"><span style="color:#5c5b55">Masa grasa</span><span style="font-weight:500">{fat['fat_kg']} kg ({fat['fat_pct']}%)</span></div>
    </div>
    <div class="scenario sc-min">
      <div class="sc-header"><div class="sc-title">Estado actual</div><span class="sc-badge sb-min">{weight['recommendation']}</span></div>
      <div class="sc-row"><span class="sc-key">% grasa actual</span><span class="sc-val">{fat['fat_pct']}% {badge(fat['global_cat'],fat_s)}</span></div>
      <div class="sc-row"><span class="sc-key">Rango fitness {dec}–{dec+9}</span><span class="sc-val">{fat['fat_ref_fitness'][0]}–{fat['fat_ref_fitness'][1]}%</span></div>
      <div class="sc-row"><span class="sc-key">Recomendación</span><span class="sc-val">{weight['recommendation']}</span></div>
    </div>
    <div class="scenario sc-opt">
      <div class="sc-header"><div class="sc-title">Ganancia muscular</div><span class="sc-badge sb-opt">+{weight['target_muscle_gain']:.0f} kg músculo</span></div>
      <div style="font-size:9px;color:#9c9a92;margin-bottom:4px">{proj_formula}</div>
      <div class="sc-row"><span class="sc-key">Peso proyectado</span><span class="sc-val">{weight['projected_weight']} kg <span class="ch-up">{delta_proj_str} kg</span></span></div>
      <div class="sc-row"><span class="sc-key">% grasa resultante</span><span class="sc-val">{weight['projected_fat_pct']}%</span></div>
      <div class="sc-row"><span class="sc-key">Masa muscular</span><span class="sc-val">{weight['projected_muscle_kg']} kg <span class="ch-up">+{weight['target_muscle_gain']:.1f} kg</span></span></div>
    </div>
  </div>
  <div>
    <div style="font-size:10px;font-weight:500;margin-bottom:6px">Tendencia {date_range}</div>
    <div style="background:#f4f2eb;border-radius:3px;padding:5px 7px;margin-bottom:8px;border:0.5px solid rgba(0,0,0,0.12);font-size:11px">
      <div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:0.5px solid rgba(0,0,0,0.12)"><span style="color:#5c5b55">Peso</span><span style="font-weight:500;color:{ew_c}">{delta_label(ew,' kg')}</span></div>
      <div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:0.5px solid rgba(0,0,0,0.12)"><span style="color:#5c5b55">Masa muscular</span><span style="font-weight:500;color:{em_c}">{delta_label(em,' kg')}</span></div>
      <div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:0.5px solid rgba(0,0,0,0.12)"><span style="color:#5c5b55">% grasa</span><span style="font-weight:500;color:{ef_c}">{delta_label(ef,'%')}</span></div>
      <div style="display:flex;justify-content:space-between;padding:2px 0"><span style="color:#5c5b55">TMB</span><span style="font-weight:500;color:{et_c}">{delta_label(et,' kcal')}</span></div>
    </div>
    {'<div class="trend-ok">'+trend_note+'</div>' if trend_note else ''}
    <div style="margin-top:10px">{chart_html}</div>
  </div>
</div>

<!-- REFERENCIAS -->
<div class="ref-section">
  <div style="font-size:11px;font-weight:500;margin-bottom:8px;text-transform:uppercase;letter-spacing:0.5px">Referencias científicas</div>
  <div class="comp-2col">
    <div>
      <div class="ref-group">
        <div class="ref-group-title">Agua corporal · Compartimentos hídricos</div>
        <div class="ref-item"><div class="ref-num">[1]</div><div>Watson PE et al. <em>Am J Clin Nutr.</em> 1980;33:27–39.<span class="ref-uses">Rangos ACT por sexo y edad.</span></div></div>
        <div class="ref-item"><div class="ref-num">[2]</div><div>Chumlea WC et al. <em>Kidney Int.</em> 2001;59:2250–2258.</div></div>
        <div class="ref-item"><div class="ref-num">[14]</div><div>De Lorenzo A et al. <em>J Appl Physiol.</em> 1997;82:1542–1558.<span class="ref-uses">ke=0.376 (mujeres); compartimentos BIA.</span></div></div>
        <div class="ref-item"><div class="ref-num">[15]</div><div>Kyle UG et al. (ESPEN). <em>Clin Nutr.</em> 2004;23:1226–1243.</div></div>
      </div>
      <div class="ref-group">
        <div class="ref-group-title">Proteína · Masa ósea · % Grasa · Segmental</div>
        <div class="ref-item"><div class="ref-num">[3]</div><div>Heymsfield SB et al. <em>Annu Rev Nutr.</em> 1997;17:527–558.</div></div>
        <div class="ref-item"><div class="ref-num">[4]</div><div>Tanita Corp. <em>Body Composition Guide.</em></div></div>
        <div class="ref-item"><div class="ref-num">[5]</div><div>Looker AC et al. <em>Osteoporos Int.</em> 1998;8:468–489.</div></div>
        <div class="ref-item"><div class="ref-num">[6]</div><div>Tanita Corp. <em>Understanding Your Measurements.</em></div></div>
        <div class="ref-item"><div class="ref-num">[7]</div><div>Gallagher D et al. <em>Am J Clin Nutr.</em> 2000;72:694–701.</div></div>
        <div class="ref-item"><div class="ref-num">[8]</div><div>Li C et al. <em>Am J Clin Nutr.</em> 2012;96:448–456.</div></div>
      </div>
    </div>
    <div>
      <div class="ref-group">
        <div class="ref-group-title">Masa muscular · Sarcopenia · Grasa visceral</div>
        <div class="ref-item"><div class="ref-num">[9]</div><div>Tanita Corp. <em>Puntuación calidad muscular.</em> Manual InnerScan Dual.<span class="ref-uses">Normal {'mujer' if p.sex=='F' else 'hombre'} {dec}–{dec+9}: {muscle['leg_score_normal'][0]}–{muscle['leg_score_normal'][1]}.</span></div></div>
        <div class="ref-item"><div class="ref-num">[10]</div><div>Scafoglieri A et al. <em>Clin Nutr.</em> 2017;36:869–875.</div></div>
        <div class="ref-item"><div class="ref-num">[11]</div><div>Petersen AM et al. <em>J Cachexia Sarcopenia Muscle.</em> 2020;11:487–496.</div></div>
        <div class="ref-item"><div class="ref-num">[12]</div><div>Janssen I et al. <em>J Appl Physiol.</em> 2000;89:465–471.</div></div>
        <div class="ref-item"><div class="ref-num">[13]</div><div>Cruz-Jentoft AJ et al. (EWGSOP2). <em>Age Ageing.</em> 2019;48:16–31.</div></div>
        <div class="ref-item"><div class="ref-num">[16]</div><div>Tanita Europe. <em>Visceral Fat Rating.</em><span class="ref-uses">Escala oficial 1–59: saludable 1–12, levemente alto 13–19, alto 20–59.</span></div></div>
        <div class="ref-item"><div class="ref-num">[17]</div><div>Despres JP. <em>Nature Rev Cardiol.</em> 2012;9:704–713.</div></div>
      </div>
    </div>
  </div>
  <div style="font-size:9px;color:#9c9a92;margin-top:8px;line-height:1.6;border-top:0.5px solid rgba(0,0,0,0.12);padding-top:6px">
    Los valores de referencia son rangos poblacionales orientativos. Este informe no reemplaza la evaluación clínica profesional. BIA puede verse afectada por hidratación, hora del día y factores individuales. MME (±5–6%) y AEC/AIC (±10–15%) son valores estimados.
  </div>
</div>

</div>
</body>
</html>"""


# ── Main ──────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--name",   required=True)
    parser.add_argument("--age",    required=True, type=int)
    parser.add_argument("--sex",    required=True, choices=["F","M"])
    parser.add_argument("--height", required=True, type=float)
    parser.add_argument("--doctor", default="")
    parser.add_argument("--output", default="reporte_tanita.pdf")
    parser.add_argument("--html",   action="store_true")
    args = parser.parse_args()

    patient = PatientInfo(args.name, args.age, args.sex, args.height)

    print(f"Cargando {args.csv}...")
    measurements = load_csv(args.csv)
    print(f"✓ {len(measurements)} mediciones ({measurements[0].date[:10]} → {measurements[-1].date[:10]})")

    print("Calculando análisis...")
    result = analyze(patient, measurements)

    print("Generando HTML...")
    html = generate_html(result, args.doctor)

    if args.html:
        hp = args.output.replace(".pdf",".html")
        open(hp,"w",encoding="utf-8").write(html)
        print(f"✓ HTML: {hp}")

    print("Generando PDF...")
    WeasyprintHTML(string=html).write_pdf(args.output)
    print(f"✓ PDF: {args.output}")


if __name__ == "__main__":
    main()
