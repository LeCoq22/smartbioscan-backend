"""
Test del motor de análisis con datos reales de Belén Beltrachini.
Ejecutar: python test_belen.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from analysis_engine import PatientInfo, analyze
from csv_parser import load_csv


def print_separator(title=""):
    w = 60
    if title:
        pad = (w - len(title) - 2) // 2
        print(f"\n{'─' * pad} {title} {'─' * pad}")
    else:
        print("─" * w)


def fmt_range(r):
    return f"{r[0]}–{r[1]}"


def run_test(csv_path: str):
    # Datos de Belén (hardcodeados para test, en producción vendrán de BD)
    patient = PatientInfo(
        name="Belén Beltrachini",
        age=48,
        sex='F',
        height_cm=158.0
    )

    measurements = load_csv(csv_path)
    print(f"\n✓ CSV cargado: {len(measurements)} mediciones")
    print(f"  Rango: {measurements[0].date[:10]} → {measurements[-1].date[:10]}")

    result = analyze(patient, measurements)
    latest = result['measurement']

    print_separator("DATOS BÁSICOS")
    print(f"  Paciente : {patient.name}")
    print(f"  Altura   : {patient.height_cm} cm | Edad: {patient.age}a | Sexo: {patient.sex}")
    print(f"  Fecha    : {latest.date[:10]}")
    print(f"  Peso     : {latest.weight_kg} kg | IMC: {latest.bmi}")

    print_separator("PARÁMETROS CLAVE")
    k = result['key_params']
    print(f"  TMB          : {k['tmb_kcal']} kcal")
    print(f"  Grasa visc.  : {k['visceral_fat']} → {k['visceral_cat']} (saludable: 1-12)")
    print(f"  Edad metab.  : {k['metabolic_age']['metabolic_age']}a ({k['metabolic_age']['label']})")
    print(f"  % Grasa glob : {k['fat_pct']}% → {k['fat_cat']} (normal: {fmt_range(k['fat_ref_normal'])}%)")
    print(f"  ACT          : {k['act_l']} L | {k['act_pct']}% → {k['act_pct_cat']}")
    print(f"  MME est.     : {k['mme_kg']} kg | IMME: {k['imme']} kg/m² → {k['imme_cat']}")
    print(f"  Score piernas: {k['leg_score']} → {k['leg_score_cat']} (normal: {fmt_range(k['leg_score_normal'])})")

    print_separator("ACT — AGUA CORPORAL TOTAL")
    a = result['act']
    print(f"  ACT kg  : {a['act_kg']} kg | Rango normal: {fmt_range(a['act_kg_range'])} → {a['act_kg_cat']}")
    print(f"  ACT %   : {a['act_pct']}% | Rango normal: {fmt_range(a['act_pct_range'])}% → {a['act_pct_cat']}")
    print(f"  AEC     : {a['aec_l']} L ({a['aec_pct']}%)")
    print(f"  AIC     : {a['aic_l']} L ({a['aic_pct']}%)")
    print(f"  Ratio   : {a['ratio']} | Normal: {fmt_range(a['ratio_normal'])} → {a['ratio_cat']}")
    if a['note']:
        print(f"  Nota: {a['note']}")

    print_separator("PROTEÍNA")
    p = result['protein']
    print(f"  MLG         : {p['mlg_kg']} kg")
    print(f"  Proteína    : {p['protein_kg']} kg | {p['protein_pct']}% → {p['pct_cat']}")
    print(f"  Ref. % norm : {fmt_range(p['ref_pct'])}%")
    if p['note']:
        print(f"  Nota: {p['note']}")

    print_separator("MASA ÓSEA")
    b = result['bone']
    print(f"  Hueso kg : {b['bone_kg']} kg | Rango: {fmt_range(b['ref_kg'])} → {b['kg_cat']}")
    print(f"  Hueso %  : {b['bone_pct']}% | Rango: {fmt_range(b['ref_pct'])}% → {b['pct_cat']}")
    if b['note']:
        print(f"  Nota: {b['note']}")

    print_separator("MASA GRASA")
    f = result['fat']
    print(f"  Grasa global: {f['fat_kg']} kg | {f['fat_pct']}% → {f['global_cat']}")
    print(f"  Refs: Atlético {fmt_range(f['fat_ref_athletic'])}% | Fitness {fmt_range(f['fat_ref_fitness'])}% | Normal {fmt_range(f['fat_ref_normal'])}%")
    print(f"\n  Segmental:")
    for seg, data in f['segmental'].items():
        print(f"    {seg:12s}: {data['pct']}% (nivel relativo {data['level']}%) → {data['cat']}")
    if f['note']:
        print(f"\n  Nota: {f['note']}")

    print_separator("MÚSCULO — MME / IMME")
    mu = result['muscle']
    print(f"  Masa musc. Tanita : {mu['muscle_kg']} kg")
    print(f"  MME estimada (÷{mu['conversion_factor']}) : {mu['mme_kg']} kg | Rango: {fmt_range(mu['mme_range'])} kg")
    print(f"  IMME              : {mu['imme']} kg/m² → {mu['imme_cat']}")
    print(f"  Umbrales EWGSOP2  : sarcopenia <{mu['imme_sarcopenia_threshold']} | normal ≥{mu['imme_normal_threshold']}")
    print(f"\n  Score piernas (prom): {mu['leg_score']} → {mu['leg_score_cat']}")
    print(f"\n  Segmental (% del ideal):")
    for seg, data in mu['segmental'].items():
        print(f"    {seg:12s}: {data['kg']} kg | {data['pct_ideal']}% del ideal ({data['ideal_kg']} kg) | calidad {data['quality']} → {data['quality_cat']}")

    print_separator("BALANCE MUSCULAR")
    ba = result['balance']
    print(f"  Brazos: Izq {ba['arm_left_kg']} kg | Der {ba['arm_right_kg']} kg | Δ{ba['arm_diff_pct']}% → {ba['arm_cat']}")
    print(f"  Piernas: Izq {ba['leg_left_kg']} kg | Der {ba['leg_right_kg']} kg | Δ{ba['leg_diff_pct']}% → {ba['leg_cat']}")

    print_separator("CONTROL DE PESO")
    w = result['weight_control']
    print(f"  Peso actual : {w['weight_kg']} kg")
    print(f"  MLG         : {w['mlg_kg']} kg")
    print(f"  Masa grasa  : {w['fat_kg']} kg ({w['fat_pct']}%)")
    print(f"  Estado      : {w['current_state']} → {w['recommendation']}")
    print(f"\n  Proyección (+{w['target_muscle_gain']} kg músculo):")
    print(f"    Peso proy. : {w['projected_weight']} kg")
    print(f"    % grasa    : {w['projected_fat_pct']}%")
    print(f"    Músculo    : {w['projected_muscle_kg']} kg")

    print_separator("EVOLUCIÓN HISTÓRICA")
    ev = result['evolution']
    if ev:
        print(f"  Período: {ev['date_first']} → {ev['date_last']}")
        print(f"  Peso    : {ev['weight_label']}")
        print(f"  Músculo : {ev['muscle_label']}")
        print(f"  % Grasa : {ev['fat_pct_label']}")
        print(f"  TMB     : {ev['tmb_label']}")
        if ev['trend_note']:
            print(f"\n  {ev['trend_note']}")

        print(f"\n  Serie completa ({len(ev['series'])} puntos):")
        for s in ev['series']:
            print(f"    {s['date']}  peso {s['weight_kg']}kg  músculo {s['muscle_kg']}kg  grasa {s['fat_pct']}%")

    print_separator()
    print("  ✓ Motor de análisis funcionando correctamente.\n")


if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "../uploads/20260420_measurements.csv"
    run_test(csv_file)
