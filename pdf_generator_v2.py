import json
import re
import os
from datetime import datetime


_MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']

def _fmt_chart_date(date_str: str) -> str:
    """'YYYY-MM-DD...' → 'jun 25'"""
    try:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d')
        return f"{_MESES[d.month - 1]} {d.strftime('%y')}"
    except Exception:
        return date_str[:7]


def _clean_label(label: str) -> str:
    """Strip leading ↓/↑ arrow and capitalize: '↓ Atlética' → 'Atlética'."""
    if not label:
        return label
    s = label.strip()
    if len(s) >= 2 and s[0] in ('↓', '↑'):
        s = s[2:]
    return s.capitalize()


def _fmt_date(date_str: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' → 'DD/MM/YYYY · HH:MM'."""
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.strftime('%d/%m/%Y · %H:%M')
    except Exception:
        return date_str[:10]


def _build_data(analysis: dict, doctor_name: str = '') -> dict:
    p       = analysis['patient']
    m       = analysis['measurement']
    fat     = analysis['fat']
    muscle  = analysis['muscle']
    bone    = analysis['bone']
    protein = analysis['protein']
    act     = analysis['act']
    balance = analysis['balance']
    evol    = analysis['evolution']

    sf = fat['segmental']
    sm = muscle['segmental']

    series   = evol.get('series', [])
    has_prev = len(series) > 1

    return {
        'full_name':          p.name,
        'measurement_date':   _fmt_date(m.date),
        'height_cm':          p.height_cm,
        'age':                p.age,
        'sex':                'Femenino' if p.sex == 'F' else 'Masculino',
        'weight_kg':          m.weight_kg,
        'bmi':                m.bmi,
        'heart_rate':         m.heart_rate,
        'bmr_kcal':           m.bmr_kcal,
        'visceral_fat':       m.visceral_fat,
        'metabolic_age':      m.metabolic_age,
        'body_fat_pct':       m.body_fat_pct,
        'fat_label':          fat['global_cat'],
        'total_body_water_l': act['act_kg'],
        'tbw_pct':            act['act_pct'],
        'muscle_mass_kg':     m.muscle_mass_kg,
        'smm_kg':             muscle['mme_kg'],
        'smm_index':          muscle['imme'],
        'bone_mass_kg':       m.bone_mass_kg,
        'bone_pct':           bone['bone_pct'],
        'bone_label':         _clean_label(bone['kg_cat']),
        'protein_kg':         protein['protein_kg'],
        'protein_pct':        protein['protein_pct'],
        'protein_label':      _clean_label(protein['kg_cat']),
        'physique_rating':    m.physique_rating,
        # Segmental fat
        'fat_trunk_pct':      sf['trunk']['pct'],
        'fat_trunk_lvl':      sf['trunk']['level'],
        'fat_trunk_label':    _clean_label(sf['trunk']['cat']),
        'fat_arm_l_pct':      sf['left_arm']['pct'],
        'fat_arm_l_lvl':      sf['left_arm']['level'],
        'fat_arm_l_label':    _clean_label(sf['left_arm']['cat']),
        'fat_arm_r_pct':      sf['right_arm']['pct'],
        'fat_arm_r_lvl':      sf['right_arm']['level'],
        'fat_arm_r_label':    _clean_label(sf['right_arm']['cat']),
        'fat_leg_l_pct':      sf['left_leg']['pct'],
        'fat_leg_l_lvl':      sf['left_leg']['level'],
        'fat_leg_l_label':    _clean_label(sf['left_leg']['cat']),
        'fat_leg_r_pct':      sf['right_leg']['pct'],
        'fat_leg_r_lvl':      sf['right_leg']['level'],
        'fat_leg_r_label':    _clean_label(sf['right_leg']['cat']),
        # Segmental muscle
        'muscle_trunk_kg':    sm['trunk']['kg'],
        'muscle_trunk_pct':   sm['trunk']['pct_ideal'],
        'muscle_arm_l_kg':    sm['left_arm']['kg'],
        'muscle_arm_l_pct':   sm['left_arm']['pct_ideal'],
        'muscle_arm_l_score': int(sm['left_arm']['quality']),
        'muscle_arm_r_kg':    sm['right_arm']['kg'],
        'muscle_arm_r_pct':   sm['right_arm']['pct_ideal'],
        'muscle_arm_r_score': int(sm['right_arm']['quality']),
        'muscle_leg_l_kg':    sm['left_leg']['kg'],
        'muscle_leg_l_pct':   sm['left_leg']['pct_ideal'],
        'muscle_leg_l_score': int(sm['left_leg']['quality']),
        'muscle_leg_r_kg':    sm['right_leg']['kg'],
        'muscle_leg_r_pct':   sm['right_leg']['pct_ideal'],
        'muscle_leg_r_score': int(sm['right_leg']['quality']),
        'muscle_quality_total': muscle['mq_total'],
        'muscle_quality_label': muscle['mq_total_cat'],
        # Balance & leg score
        'balance_arm_pct':    balance['arm_diff_pct'],
        'balance_leg_pct':    balance['leg_diff_pct'],
        'leg_score':          muscle['leg_score'],
        # Water compartments
        'ecw_l':              act['aec_l'],
        'icw_l':              act['aic_l'],
        'ecw_icw_ratio':      act['ratio'],
        # Evolution — serie completa para el gráfico (N puntos)
        'history_series': [
            {'date': s['date'][:10], 'label': _fmt_chart_date(s['date']),
             'weight_kg': s['weight_kg'], 'muscle_kg': s['muscle_kg'], 'fat_pct': s['fat_pct']}
            for s in series
        ] if series else None,
        # Mantener prev_* para el bloque "Tendencia" (texto, no gráfico)
        'prev_weight':        series[0]['weight_kg'] if has_prev else None,
        'prev_muscle':        series[0]['muscle_kg']  if has_prev else None,
        'prev_fat_pct':       series[0]['fat_pct']    if has_prev else None,
        'prev_bmr':           series[0]['tmb']         if has_prev else None,
        'trend_from':         evol.get('date_first'),
        'trend_to':           evol.get('date_last'),
        # Nutritionist — display_signature se muestra tal cual, sin prefijo
        'display_signature':  doctor_name,
    }


def generate_html_v2(analysis: dict, doctor_name: str = '') -> str:
    template = os.path.join(os.path.dirname(__file__), 'smartbioscan-reporte-playground.html')
    with open(template, encoding='utf-8') as f:
        html = f.read()

    data      = _build_data(analysis, doctor_name)
    data_json = json.dumps(data, ensure_ascii=False)

    html = re.sub(
        r'var DEMO\s*=\s*\{[\s\S]*?\};',
        f'var DEMO = {data_json};',
        html, count=1,
    )
    html = html.replace('<style>', '<style>#controls{display:none!important}', 1)

    return html
