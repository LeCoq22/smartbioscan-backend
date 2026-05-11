"""
Motor de análisis de composición corporal Tanita
Basado en el reporte de Belén Beltrachini (16/04/2026)

Lógica completa:
- ACT (Agua Corporal Total): cantidad + porcentaje + AEC/AIC/Ratio
- Proteína
- Masa ósea
- Masa grasa segmental
- Masa muscular + MME/IMME (sarcopenia EWGSOP2)
- Análisis músculo-grasa
- Grasa visceral
- Balance muscular y puntuación de piernas
- Control de peso y evolución histórica
"""

import math
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# ESTRUCTURAS DE DATOS
# ─────────────────────────────────────────────

@dataclass
class PatientInfo:
    """Datos personales del paciente."""
    name: str
    age: int          # años
    sex: str          # 'F' | 'M'
    height_cm: float


@dataclass
class TanitaMeasurement:
    """Una medición cruda del CSV del scraper de MyTanita."""
    date: str                     # ISO string "YYYY-MM-DD HH:MM:SS"
    weight_kg: float
    bmi: float
    body_fat_pct: float
    visceral_fat: float
    muscle_mass_kg: float         # masa muscular total Tanita (incluye lisa + cardíaco)
    muscle_quality: float         # score global
    bone_mass_kg: float
    bmr_kcal: float               # TMB
    metabolic_age: int
    body_water_pct: float         # % agua corporal
    physique_rating: int

    # Segmental - masa muscular (kg)
    muscle_right_arm: float
    muscle_left_arm: float
    muscle_right_leg: float
    muscle_left_leg: float
    muscle_trunk: float

    # Segmental - calidad muscular (score)
    quality_right_arm: float
    quality_left_arm: float
    quality_right_leg: float
    quality_left_leg: float
    quality_trunk: float

    # Segmental - % grasa
    fat_pct_right_arm: float
    fat_pct_left_arm: float
    fat_pct_right_leg: float
    fat_pct_left_leg: float
    fat_pct_trunk: float

    heart_rate: Optional[float] = None


# ─────────────────────────────────────────────
# TABLAS DE REFERENCIA
# ─────────────────────────────────────────────

def get_act_references(sex: str, weight_kg: float) -> dict:
    """
    Agua Corporal Total (ACT) - Referencias por sexo.
    Fuente: Watson PE et al. Am J Clin Nutr. 1980 [1]
    Los rangos en kg son proporcionales al peso, aproximados al rango normal 50-60% F / 55-65% M.
    """
    if sex == 'F':
        pct_normal = (45.0, 60.0)
        # Rango en kg: 32-48 kg (ejemplo Belén ~54 kg)
        kg_low = weight_kg * 0.45
        kg_high = weight_kg * 0.60
    else:
        pct_normal = (50.0, 65.0)
        kg_low = weight_kg * 0.50
        kg_high = weight_kg * 0.65

    return {
        'pct_normal': pct_normal,
        'kg_range': (round(kg_low, 1), round(kg_high, 1))
    }


def get_aec_aic_references(sex: str = 'F') -> dict:
    """
    Compartimentos hídricos.
    Fuente: De Lorenzo A et al. J Appl Physiol. 1997 [14]
    Ratio AEC/AIC normal: 0.55–0.65
    ke (constante De Lorenzo): mujeres 0.376, hombres 0.316
    """
    ke = 0.376 if sex == 'F' else 0.316
    return {
        'ratio_normal': (0.55, 0.65),
        'ke': ke,
        'ratio_low_label': 'Deshidratación',
        'ratio_high_label': 'Retención hídrica'
    }


def get_protein_references(sex: str) -> dict:
    """
    Proteína corporal por sexo.
    Fuente: Heymsfield SB et al. Annu Rev Nutr. 1997 [3]; Tanita Body Composition Guide [4]
    Las mujeres tienen estructuralmente mayor % grasa → menor protein_pct sobre peso total.
    """
    if sex == 'F':
        return {
            'kg_range':   (7.5, 9.5),
            'pct_normal': (10.0, 14.0),
        }
    else:
        return {
            'kg_range':   (9.5, 13.0),
            'pct_normal': (15.0, 20.0),
        }


def get_bone_mass_references(sex: str, age: int) -> dict:
    """
    Masa ósea por sexo y grupo etario.
    Fuente: Looker AC et al. Osteoporos Int. 1998 [5]; Tanita Understanding Your Measurements [6]
    """
    # Referencia mujer
    if sex == 'F':
        if age < 40:
            return {'kg_range': (2.5, 3.5), 'pct_normal': (3.0, 5.0)}
        elif age < 50:
            return {'kg_range': (2.3, 3.2), 'pct_normal': (3.0, 5.0)}
        elif age < 60:
            return {'kg_range': (2.2, 3.0), 'pct_normal': (3.0, 5.0)}
        else:
            return {'kg_range': (2.0, 2.8), 'pct_normal': (3.0, 5.0)}
    else:
        if age < 40:
            return {'kg_range': (3.0, 4.5), 'pct_normal': (3.0, 5.0)}
        elif age < 50:
            return {'kg_range': (2.8, 4.2), 'pct_normal': (3.0, 5.0)}
        else:
            return {'kg_range': (2.5, 4.0), 'pct_normal': (3.0, 5.0)}


def get_fat_references(sex: str, age: int) -> dict:
    """
    % Grasa global y segmental por sexo y grupo etario.
    Fuente: Gallagher D et al. Am J Clin Nutr. 2000 [7]; Li C et al. Am J Clin Nutr. 2012 [8]
    """
    # Referencia global mujer
    if sex == 'F':
        if age < 40:
            refs = {'normal': (21, 33), 'fitness': (18, 24), 'athletic': (14, 20)}
        elif age < 50:
            refs = {'normal': (23, 35), 'fitness': (18, 24), 'athletic': (15, 21)}
        elif age < 60:
            refs = {'normal': (24, 36), 'fitness': (18, 25), 'athletic': (16, 22)}
        else:
            refs = {'normal': (25, 37), 'fitness': (19, 26), 'athletic': (17, 23)}
    else:
        if age < 40:
            refs = {'normal': (8, 20), 'fitness': (10, 16), 'athletic': (6, 13)}
        elif age < 50:
            refs = {'normal': (11, 22), 'fitness': (11, 17), 'athletic': (8, 15)}
        elif age < 60:
            refs = {'normal': (13, 25), 'fitness': (12, 19), 'athletic': (10, 17)}
        else:
            refs = {'normal': (15, 27), 'fitness': (14, 21), 'athletic': (11, 19)}

    # Segmental mujer (aproximado por segmento)
    if sex == 'F':
        segmental = {
            'trunk':     {'ref': (20, 28), 'ideal': 24},
            'arm':       {'ref': (23, 33), 'ideal': 28},
            'leg':       {'ref': (26, 38), 'ideal': 32},
        }
    else:
        segmental = {
            'trunk':     {'ref': (10, 20), 'ideal': 15},
            'arm':       {'ref': (8, 18),  'ideal': 13},
            'leg':       {'ref': (15, 25), 'ideal': 20},
        }

    refs['segmental'] = segmental
    return refs


def get_visceral_fat_labels() -> dict:
    """
    Escala grasa visceral Tanita (1-59).
    Fuente: Tanita Europe. Visceral Fat Rating [16]; Despres JP. Nature Rev Cardiol. 2012 [17]
    """
    return {
        'saludable': (1, 12),
        'levemente_alto': (13, 19),
        'alto': (20, 59),
    }


def get_muscle_quality_references(sex: str, age: int) -> dict:
    """
    Puntuación de calidad muscular (score Tanita).
    Fuente: Tanita Corp. Manual InnerScan Dual [9]
    Normal mujer 40-49: 42-67
    """
    if sex == 'F':
        if age < 30:
            return {'normal': (52, 75)}
        elif age < 40:
            return {'normal': (47, 70)}
        elif age < 50:
            return {'normal': (42, 67)}
        elif age < 60:
            return {'normal': (38, 62)}
        else:
            return {'normal': (33, 57)}
    else:
        if age < 30:
            return {'normal': (60, 85)}
        elif age < 40:
            return {'normal': (55, 80)}
        elif age < 50:
            return {'normal': (50, 75)}
        elif age < 60:
            return {'normal': (44, 69)}
        else:
            return {'normal': (38, 63)}


def classify_mq_total(mq: float) -> str:
    """Clasifica MQ global Tanita. Rangos iguales por sexo. Fuente: Tanita Corp. [9]"""
    if mq >= 80: return 'Excelente'
    if mq >= 60: return 'Bueno'
    if mq >= 50: return 'Normal'
    return 'Bajo'


def get_imme_threshold(sex: str) -> dict:
    """
    Índice de Masa Muscular Esquelética (IMME) - umbrales EWGSOP2 2019.
    Fuente: Cruz-Jentoft AJ et al. Age Ageing. 2019 [13]
    MME estimada con factor de conversión Janssen I et al. J Appl Physiol. 2000 [12]
    """
    if sex == 'F':
        return {
            'sarcopenia_threshold': 6.0,  # kg/m²
            'normal_threshold': 7.0,      # kg/m²  (zona gris 6.0-7.0)
            'conversion_factor_range': (1.17, 1.22),
            'conversion_factor': 1.19,    # punto medio
        }
    else:
        return {
            'sarcopenia_threshold': 7.0,
            'normal_threshold': 8.5,
            'conversion_factor_range': (1.17, 1.22),
            'conversion_factor': 1.19,
        }


# ─────────────────────────────────────────────
# FUNCIONES DE CÁLCULO
# ─────────────────────────────────────────────

def classify_range(value: float, normal_range: tuple,
                   low_label='Bajo', normal_label='Normal', high_label='Alto') -> str:
    """Clasifica un valor en bajo/normal/alto dado un rango normal."""
    lo, hi = normal_range
    if value < lo:
        return low_label
    elif value > hi:
        return high_label
    return normal_label


def classify_fat_pct(value: float, refs: dict) -> str:
    """Clasifica % grasa: Atlético / Fitness / Normal / Alto."""
    if value <= refs['athletic'][1]:
        return 'Atlético'
    elif value <= refs['fitness'][1]:
        return 'Fitness'
    elif value <= refs['normal'][1]:
        return 'Normal'
    return 'Alto'


def classify_visceral_fat(level: float) -> str:
    labels = get_visceral_fat_labels()
    if labels['saludable'][0] <= level <= labels['saludable'][1]:
        return 'Saludable'
    elif labels['levemente_alto'][0] <= level <= labels['levemente_alto'][1]:
        return 'Levemente alto'
    return 'Alto'


# ── ACT ──────────────────────────────────────

def compute_act(m: TanitaMeasurement, patient: PatientInfo) -> dict:
    """
    Agua Corporal Total.
    ACT_kg = weight * body_water_pct / 100
    AEC/AIC se estiman con modelo De Lorenzo 1997 (ke=0.316).
    Ratio AEC/AIC normal: 0.55–0.65
    """
    act_kg = round(m.weight_kg * m.body_water_pct / 100, 2)
    refs = get_act_references(patient.sex, m.weight_kg)
    aec_ref = get_aec_aic_references(patient.sex)
    ke = aec_ref['ke']

    # Estimación AEC/AIC (De Lorenzo 1997): AEC = ke * ACT
    aec_l = round(ke * act_kg, 2)
    aic_l = round(act_kg - aec_l, 2)
    ratio = round(aec_l / aic_l, 2) if aic_l > 0 else 0

    pct_cat = classify_range(m.body_water_pct, refs['pct_normal'])
    kg_cat  = classify_range(act_kg, refs['kg_range'])
    ratio_cat = classify_range(ratio, aec_ref['ratio_normal'],
                               low_label='Deshidratación',
                               normal_label='Normal',
                               high_label='Retención hídrica')

    aec_pct = round(aec_l / act_kg * 100) if act_kg > 0 else 0
    aic_pct = round(aic_l / act_kg * 100) if act_kg > 0 else 0

    return {
        'act_kg': act_kg,
        'act_pct': m.body_water_pct,
        'act_kg_range': refs['kg_range'],
        'act_pct_range': refs['pct_normal'],
        'act_kg_cat': kg_cat,
        'act_pct_cat': pct_cat,
        'aec_l': aec_l,
        'aic_l': aic_l,
        'aec_pct': aec_pct,
        'aic_pct': aic_pct,
        'ratio': ratio,
        'ratio_normal': aec_ref['ratio_normal'],
        'ratio_cat': ratio_cat,
        'ke': ke,
        'note': (
            f"El kg de ACT está {'dentro' if kg_cat == 'Normal' else 'fuera'} del rango normal. "
            f"El % corporal ({m.body_water_pct}%) está {'dentro' if pct_cat == 'Normal' else 'fuera'} del rango normal."
        )
    }


# ── PROTEÍNA ─────────────────────────────────

def compute_protein(m: TanitaMeasurement, sex: str) -> dict:
    """
    Proteína ≈ 20% de la MLG (Heymsfield 1997).
    MLG = Peso − Masa grasa.
    Rangos de referencia diferenciados por sexo.
    """
    mass_fat_kg = round(m.weight_kg * m.body_fat_pct / 100, 2)
    mlg_kg      = round(m.weight_kg - mass_fat_kg, 2)
    protein_kg  = round(mlg_kg * 0.20, 2)
    protein_pct = round(protein_kg / m.weight_kg * 100, 1) if m.weight_kg > 0 else 0.0

    refs    = get_protein_references(sex)
    pct_cat = classify_range(protein_pct, refs['pct_normal'])
    kg_cat  = classify_range(protein_kg,  refs['kg_range'])

    note = ""
    if kg_cat == 'Alto':
        note = f"Alta masa muscular: el % ({protein_pct}%) refleja excelente composición magra."

    return {
        'mlg_kg':      mlg_kg,
        'protein_kg':  protein_kg,
        'protein_pct': protein_pct,
        'ref_kg':      refs['kg_range'],
        'ref_pct':     refs['pct_normal'],
        'pct_cat':     pct_cat,
        'kg_cat':      kg_cat,
        'note':        note,
    }


# ── MASA ÓSEA ────────────────────────────────

def compute_bone(m: TanitaMeasurement, patient: PatientInfo) -> dict:
    """Masa ósea con referencia por sexo y edad."""
    bone_pct = round(m.bone_mass_kg / m.weight_kg * 100, 2) if m.weight_kg > 0 else 0.0
    refs = get_bone_mass_references(patient.sex, patient.age)

    kg_cat  = classify_range(m.bone_mass_kg, refs['kg_range'],
                              low_label='↓ leve', normal_label='Normal', high_label='Alto')
    pct_cat = classify_range(bone_pct, refs['pct_normal'])

    note = ""
    if m.bone_mass_kg < refs['kg_range'][0]:
        note = (f"kg levemente bajo ({m.bone_mass_kg} vs mín. {refs['kg_range'][0]}). "
                f"El % ({bone_pct}%) está dentro del rango normal. Vigilar tendencia.")

    return {
        'bone_kg': m.bone_mass_kg,
        'bone_pct': bone_pct,
        'ref_kg': refs['kg_range'],
        'ref_pct': refs['pct_normal'],
        'kg_cat': kg_cat,
        'pct_cat': pct_cat,
        'note': note
    }


# ── MASA GRASA ───────────────────────────────

def compute_fat(m: TanitaMeasurement, patient: PatientInfo) -> dict:
    """Masa grasa global y segmental."""
    fat_kg = round(m.weight_kg * m.body_fat_pct / 100, 2)
    refs = get_fat_references(patient.sex, patient.age)

    global_cat = classify_fat_pct(m.body_fat_pct, refs)

    # Nivel relativo al ideal segmental (0-200%, donde 100% = ideal)
    seg = refs['segmental']

    def seg_level(fat_pct, segment_key):
        ideal = seg[segment_key]['ideal']
        return round(fat_pct / ideal * 100) if ideal > 0 else 0

    def seg_cat(fat_pct, segment_key):
        ref_range = seg[segment_key]['ref']
        return classify_range(fat_pct, ref_range,
                              low_label='↓ Atlética', normal_label='↓ Normal', high_label='Alto')

    trunk_level  = seg_level(m.fat_pct_trunk, 'trunk')
    r_arm_level  = seg_level(m.fat_pct_right_arm, 'arm')
    l_arm_level  = seg_level(m.fat_pct_left_arm, 'arm')
    r_leg_level  = seg_level(m.fat_pct_right_leg, 'leg')
    l_leg_level  = seg_level(m.fat_pct_left_leg, 'leg')

    note = ""
    if global_cat in ('Atlético', 'Fitness'):
        note = (f"{m.body_fat_pct}% corresponde a rango {global_cat.lower()}-fitness "
                f"para {'mujer' if patient.sex == 'F' else 'hombre'} de {patient.age} años "
                f"— por debajo del rango normal de su grupo de edad.")

    return {
        'fat_kg': fat_kg,
        'fat_pct': m.body_fat_pct,
        'fat_ref_normal': refs['normal'],
        'fat_ref_fitness': refs['fitness'],
        'fat_ref_athletic': refs['athletic'],
        'global_cat': global_cat,
        'segmental': {
            'trunk':     {'pct': m.fat_pct_trunk,     'level': trunk_level,  'ref': seg['trunk']['ref'],  'ideal': seg['trunk']['ideal'],  'cat': seg_cat(m.fat_pct_trunk, 'trunk')},
            'right_arm': {'pct': m.fat_pct_right_arm, 'level': r_arm_level,  'ref': seg['arm']['ref'],    'ideal': seg['arm']['ideal'],    'cat': seg_cat(m.fat_pct_right_arm, 'arm')},
            'left_arm':  {'pct': m.fat_pct_left_arm,  'level': l_arm_level,  'ref': seg['arm']['ref'],    'ideal': seg['arm']['ideal'],    'cat': seg_cat(m.fat_pct_left_arm, 'arm')},
            'right_leg': {'pct': m.fat_pct_right_leg, 'level': r_leg_level,  'ref': seg['leg']['ref'],    'ideal': seg['leg']['ideal'],    'cat': seg_cat(m.fat_pct_right_leg, 'leg')},
            'left_leg':  {'pct': m.fat_pct_left_leg,  'level': l_leg_level,  'ref': seg['leg']['ref'],    'ideal': seg['leg']['ideal'],    'cat': seg_cat(m.fat_pct_left_leg, 'leg')},
        },
        'note': note
    }


# ── MÚSCULO + MME/IMME ───────────────────────

def compute_muscle(m: TanitaMeasurement, patient: PatientInfo) -> dict:
    """
    Masa muscular, MME estimada e IMME.
    MME = Masa muscular Tanita / factor_conversion (Janssen 2000)
    IMME = MME / altura²
    Umbral sarcopenia EWGSOP2 (Cruz-Jentoft 2019): F <6.0, M <7.0 kg/m²
    """
    imme_refs = get_imme_threshold(patient.sex)
    factor = imme_refs['conversion_factor']
    height_m = patient.height_cm / 100

    mme_kg = round(m.muscle_mass_kg / factor, 1) if factor > 0 else 0.0
    imme = round(mme_kg / (height_m ** 2), 2) if height_m > 0 else 0.0

    # Rango MME (±6%)
    mme_low  = round(mme_kg * 0.94, 1)
    mme_high = round(mme_kg * 1.06, 1)

    if imme >= imme_refs['normal_threshold']:
        imme_cat = 'Sin sarcopenia'
    elif imme >= imme_refs['sarcopenia_threshold']:
        imme_cat = 'Zona gris'
    else:
        imme_cat = 'Sarcopenia'

    # Puntuación calidad muscular piernas (score Tanita)
    leg_score = round((m.quality_right_leg + m.quality_left_leg) / 2, 1)
    mq_refs = get_muscle_quality_references(patient.sex, patient.age)

    leg_score_cat = classify_range(leg_score, mq_refs['normal'],
                                   low_label='Bajo', normal_label='Normal', high_label='↑ Alta')

    # Segmental: % del ideal por altura
    # Ideal por segmento (valores Tanita para 158 cm mujer, escalables)
    # Escala proporcional: ideal_arm = 2.20, ideal_leg = 7.50, ideal_trunk = 22.50 (para 158cm F)
    # Factor de escala por altura
    height_factor = patient.height_cm / 158.0  # referencia base 158 cm

    if patient.sex == 'F':
        ideal_arm_kg   = round(2.20 * height_factor, 2)
        ideal_leg_kg   = round(7.50 * height_factor, 2)
        ideal_trunk_kg = round(22.50 * height_factor, 2)
    else:
        ideal_arm_kg   = round(3.50 * height_factor, 2)
        ideal_leg_kg   = round(10.0 * height_factor, 2)
        ideal_trunk_kg = round(28.0 * height_factor, 2)

    def seg_pct_ideal(actual, ideal):
        return round(actual / ideal * 100) if ideal > 0 else 0

    segmental = {
        'trunk':     {'kg': m.muscle_trunk,      'ideal_kg': ideal_trunk_kg, 'pct_ideal': seg_pct_ideal(m.muscle_trunk,      ideal_trunk_kg), 'quality': m.quality_trunk},
        'right_arm': {'kg': m.muscle_right_arm,  'ideal_kg': ideal_arm_kg,   'pct_ideal': seg_pct_ideal(m.muscle_right_arm,  ideal_arm_kg),   'quality': m.quality_right_arm},
        'left_arm':  {'kg': m.muscle_left_arm,   'ideal_kg': ideal_arm_kg,   'pct_ideal': seg_pct_ideal(m.muscle_left_arm,   ideal_arm_kg),   'quality': m.quality_left_arm},
        'right_leg': {'kg': m.muscle_right_leg,  'ideal_kg': ideal_leg_kg,   'pct_ideal': seg_pct_ideal(m.muscle_right_leg,  ideal_leg_kg),   'quality': m.quality_right_leg},
        'left_leg':  {'kg': m.muscle_left_leg,   'ideal_kg': ideal_leg_kg,   'pct_ideal': seg_pct_ideal(m.muscle_left_leg,   ideal_leg_kg),   'quality': m.quality_left_leg},
    }

    # Clasificación de calidad segmental
    for seg_key, seg_data in segmental.items():
        seg_data['quality_cat'] = classify_range(
            seg_data['quality'], mq_refs['normal'],
            low_label='Bajo', normal_label='Normal', high_label='↑ Normal'
        )

    mq_total     = m.muscle_quality
    mq_total_cat = classify_mq_total(mq_total)

    return {
        'muscle_kg': m.muscle_mass_kg,
        'mme_kg': mme_kg,
        'mme_range': (mme_low, mme_high),
        'imme': imme,
        'imme_cat': imme_cat,
        'imme_sarcopenia_threshold': imme_refs['sarcopenia_threshold'],
        'imme_normal_threshold': imme_refs['normal_threshold'],
        'conversion_factor': factor,
        'conversion_factor_range': imme_refs['conversion_factor_range'],
        'leg_score': leg_score,
        'leg_score_cat': leg_score_cat,
        'leg_score_normal': mq_refs['normal'],
        'segmental': segmental,
        'ideal_arm_kg': ideal_arm_kg,
        'ideal_leg_kg': ideal_leg_kg,
        'ideal_trunk_kg': ideal_trunk_kg,
        'mq_total': mq_total,
        'mq_total_cat': mq_total_cat,
    }


# ── BALANCE MUSCULAR ──────────────────────────

def compute_muscle_balance(m: TanitaMeasurement) -> dict:
    """
    Asimetría izquierda-derecha por segmento.
    Normal: diferencia < 10%.
    """
    arm_diff_pct = round(abs(m.muscle_right_arm - m.muscle_left_arm) /
                         max(m.muscle_right_arm, m.muscle_left_arm) * 100, 1)
    leg_diff_pct = round(abs(m.muscle_right_leg - m.muscle_left_leg) /
                         max(m.muscle_right_leg, m.muscle_left_leg) * 100, 1)

    ARM_THRESHOLD = 10.0
    LEG_THRESHOLD = 10.0

    return {
        'arm_left_kg': m.muscle_left_arm,
        'arm_right_kg': m.muscle_right_arm,
        'arm_diff_pct': arm_diff_pct,
        'arm_cat': 'Normal' if arm_diff_pct < ARM_THRESHOLD else 'Asimétrico',
        'arm_label': 'Simétrico' if arm_diff_pct < ARM_THRESHOLD else f'Δ{arm_diff_pct}%',
        'leg_left_kg': m.muscle_left_leg,
        'leg_right_kg': m.muscle_right_leg,
        'leg_diff_pct': leg_diff_pct,
        'leg_cat': 'Normal' if leg_diff_pct < LEG_THRESHOLD else 'Asimétrico',
        'leg_label': 'Simétrico' if leg_diff_pct < LEG_THRESHOLD else f'Δ{leg_diff_pct}%',
    }


# ── EDAD METABÓLICA ───────────────────────────

def compute_metabolic_age(m: TanitaMeasurement, patient: PatientInfo) -> dict:
    diff = patient.age - m.metabolic_age
    if diff > 0:
        label = f"{diff} años menos que la real"
    elif diff < 0:
        label = f"{abs(diff)} años más que la real"
    else:
        label = "Igual a la edad real"
    return {
        'metabolic_age': m.metabolic_age,
        'real_age': patient.age,
        'diff': diff,
        'label': label
    }


# ── PARÁMETROS CLAVE (resumen superior) ───────

def compute_key_params(m: TanitaMeasurement, patient: PatientInfo,
                       act_result: dict, muscle_result: dict,
                       fat_result: dict) -> dict:
    """Resumen de los parámetros clave del encabezado del reporte."""
    visceral_cat = classify_visceral_fat(m.visceral_fat)
    meta_age = compute_metabolic_age(m, patient)

    return {
        'tmb_kcal': m.bmr_kcal,
        'visceral_fat': m.visceral_fat,
        'visceral_cat': visceral_cat,
        'visceral_range': get_visceral_fat_labels()['saludable'],
        'metabolic_age': meta_age,
        'fat_pct': m.body_fat_pct,
        'fat_cat': fat_result['global_cat'],
        'fat_ref_normal': fat_result['fat_ref_normal'],
        'act_l': act_result['act_kg'],   # litros = kg para agua
        'act_pct': act_result['act_pct'],
        'act_pct_cat': act_result['act_pct_cat'],
        'mme_kg': muscle_result['mme_kg'],
        'imme': muscle_result['imme'],
        'imme_cat': muscle_result['imme_cat'],
        'leg_score': muscle_result['leg_score'],
        'leg_score_cat': muscle_result['leg_score_cat'],
        'leg_score_normal': muscle_result['leg_score_normal'],
    }


# ── CONTROL DE PESO ───────────────────────────

def compute_weight_control(m: TanitaMeasurement, patient: PatientInfo,
                            fat_result: dict) -> dict:
    """
    Control de peso por composición (sin uso de IMC).
    MLG real y objetivo de +2 kg de músculo (recomendación Nutri).
    """
    fat_kg = fat_result['fat_kg']
    mlg_kg = round(m.weight_kg - fat_kg, 2)

    # Peso proyectado con +2 kg músculo
    target_muscle_gain = 2.0
    projected_weight = round(mlg_kg + target_muscle_gain + fat_kg * (1 - 0.0), 2)
    # Si gana músculo manteniendo grasa:
    projected_weight_v2 = round(mlg_kg + target_muscle_gain + fat_kg, 2)
    projected_fat_pct = round(fat_kg / projected_weight_v2 * 100, 1) if projected_weight_v2 > 0 else 0.0
    projected_muscle_kg = round(m.muscle_mass_kg + target_muscle_gain, 1)

    # Estado actual de grasa
    fat_refs = get_fat_references(patient.sex, patient.age)
    current_state = fat_result['global_cat']

    if current_state in ('Atlético', 'Fitness'):
        recommendation = 'Mantener peso y músculo'
    elif current_state == 'Normal':
        recommendation = 'Mantener composición'
    else:
        recommendation = 'Reducir masa grasa'

    return {
        'weight_kg': m.weight_kg,
        'mlg_kg': mlg_kg,
        'fat_kg': fat_kg,
        'fat_pct': m.body_fat_pct,
        'current_state': current_state,
        'fat_range_fitness': fat_refs['fitness'],
        'recommendation': recommendation,
        'target_muscle_gain': target_muscle_gain,
        'projected_weight': projected_weight_v2,
        'projected_fat_pct': projected_fat_pct,
        'projected_muscle_kg': projected_muscle_kg,
    }


# ── EVOLUCIÓN HISTÓRICA ───────────────────────

def compute_evolution(measurements: list[TanitaMeasurement]) -> dict:
    """
    Calcula tendencias entre primera y última medición.
    Retorna deltas para peso, músculo, % grasa y TMB.
    """
    if len(measurements) < 2:
        return {}

    first = measurements[0]
    last  = measurements[-1]

    fat_first = round(first.weight_kg * first.body_fat_pct / 100, 2)
    fat_last  = round(last.weight_kg  * last.body_fat_pct  / 100, 2)

    delta_weight  = round(last.weight_kg    - first.weight_kg, 2)
    delta_muscle  = round(last.muscle_mass_kg - first.muscle_mass_kg, 2)
    delta_fat_pct = round(last.body_fat_pct - first.body_fat_pct, 1)
    delta_tmb     = round(last.bmr_kcal     - first.bmr_kcal, 0)

    def trend_label(delta):
        if delta > 0:
            return f"+{delta} ↑"
        elif delta < 0:
            return f"{delta} ↓"
        return "= Sin cambio"

    # Construir series para gráfico
    series = []
    for mes in measurements:
        fat_kg = round(mes.weight_kg * mes.body_fat_pct / 100, 2)
        series.append({
            'date': mes.date[:10],
            'weight_kg': mes.weight_kg,
            'muscle_kg': mes.muscle_mass_kg,
            'fat_pct': mes.body_fat_pct,
            'fat_kg': fat_kg,
            'tmb': mes.bmr_kcal,
        })

    # Detectar mejora consistente
    improved = (delta_muscle > 0 and delta_fat_pct < 0 and delta_tmb > 0)
    trend_note = "✓ Mejora consistente: menos grasa, más músculo, mejor TMB." if improved else ""

    return {
        'date_first': first.date[:10],
        'date_last':  last.date[:10],
        'delta_weight':  delta_weight,
        'delta_muscle':  delta_muscle,
        'delta_fat_pct': delta_fat_pct,
        'delta_tmb':     int(delta_tmb),
        'weight_label':  trend_label(delta_weight),
        'muscle_label':  trend_label(delta_muscle),
        'fat_pct_label': trend_label(delta_fat_pct),
        'tmb_label':     trend_label(int(delta_tmb)),
        'trend_note':    trend_note,
        'series':        series,
    }


# ─────────────────────────────────────────────
# FUNCIÓN PRINCIPAL: análisis completo
# ─────────────────────────────────────────────

def analyze(patient: PatientInfo,
            measurements: list[TanitaMeasurement]) -> dict:
    """
    Ejecuta el análisis completo sobre todas las mediciones.
    Retorna dict con la última medición analizada + evolución.

    Args:
        patient: datos personales del paciente
        measurements: lista ordenada cronológicamente de mediciones

    Returns:
        dict con todos los módulos de análisis
    """
    if not measurements:
        raise ValueError("Se necesita al menos una medición.")

    latest = measurements[-1]

    act     = compute_act(latest, patient)
    protein = compute_protein(latest, patient.sex)
    bone    = compute_bone(latest, patient)
    fat     = compute_fat(latest, patient)
    muscle  = compute_muscle(latest, patient)
    balance = compute_muscle_balance(latest)
    weight  = compute_weight_control(latest, patient, fat)
    key     = compute_key_params(latest, patient, act, muscle, fat)
    evol    = compute_evolution(measurements)

    _physique_labels = {
        1: "Obeso",
        2: "Sobrepeso, masa baja",
        3: "Sobrepeso, masa alta",
        4: "Estándar, masa baja",
        5: "Estándar",
        6: "Estándar, masa alta",
        7: "Delgado",
        8: "Delgado y musculoso",
        9: "Muy musculoso",
    }
    pr = latest.physique_rating
    physique = {
        'rating': pr,
        'label':  _physique_labels.get(pr, f"Rating {pr}"),
    }

    return {
        'patient':    patient,
        'measurement': latest,
        'key_params': key,
        'act':        act,
        'protein':    protein,
        'bone':       bone,
        'fat':        fat,
        'muscle':     muscle,
        'balance':    balance,
        'weight_control': weight,
        'evolution':  evol,
        'physique':   physique,
    }
