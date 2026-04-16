from flask import Flask, render_template, request, jsonify
import sqlite3
import json
import re

app = Flask(__name__)
DB_NAME = "skat_bot.db"

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (СКФ, лабораторные, стратификация) ----------
def calculate_crcl(age, weight, creatinine, sex):
    if creatinine > 20:
        scr_mgdl = creatinine / 88.4
    else:
        scr_mgdl = creatinine
    crcl = (140 - age) * weight / (72 * scr_mgdl)
    if sex == 'female':
        crcl *= 0.85
    return max(crcl, 5.0)

def interpret_lab(leukocytes, esr, pct):
    result = []
    if leukocytes == "<4.0":
        result.append("Лейкопения — возможна вирусная инфекция, сепсис.")
    elif leukocytes == "4.0-10.0":
        result.append("Лейкоциты в норме — не исключает бактериальную инфекцию.")
    elif leukocytes == "10.0-15.0":
        result.append("Умеренный лейкоцитоз — характерен для бактериальной инфекции.")
    elif leukocytes == ">15.0":
        result.append("Выраженный лейкоцитоз — высокая вероятность бактериальной инфекции.")
    if esr == "<10":
        result.append("СОЭ в норме — маловероятно активное воспаление.")
    elif esr == "10-30":
        result.append("СОЭ умеренно повышена.")
    elif esr == "30-60":
        result.append("СОЭ значительно повышена — характерно для бактериальных инфекций.")
    elif esr == ">60":
        result.append("СОЭ резко повышена — тяжелая инфекция, сепсис.")
    if pct == "<0.1":
        result.append("Прокальцитонин <0.1 — бактериальная инфекция маловероятна.")
    elif pct == "0.1-0.25":
        result.append("Прокальцитонин 0.1-0.25 — локальная инфекция возможна.")
    elif pct == "0.25-0.5":
        result.append("Прокальцитонин 0.25-0.5 — высокая вероятность бактериальной инфекции.")
    elif pct == ">0.5":
        result.append("Прокальцитонин >0.5 — бактериальная инфекция очень вероятна.")
    return result

def interpret_sofa(sofa):
    if sofa < 2: return "Низкий риск смертности (<10%)"
    elif sofa < 6: return "Умеренный риск (10-20%)"
    elif sofa < 12: return "Высокий риск (30-40%)"
    else: return "Очень высокий риск (>50%)"

def determine_risk_level(hospital_days, risk_factors):
    if hospital_days < 2: return "community"
    elif hospital_days <= 7: return "early_nosocomial"
    else:
        has_mrsa = any(f in ["Предшествующие антибиотики (цефалоспорины/фторхинолоны)", "Колонизация/инфекция МРЗС в анамнезе", "Катетер центральной вены >7 дней"] for f in risk_factors)
        has_pseudomonas = any(f in ["ИВЛ > 5 дней", "Нейтропения (<500)", "Длительная госпитализация (>14 дней)"] for f in risk_factors)
        if has_mrsa: return "late_mrsa"
        elif has_pseudomonas: return "late_pseudomonas"
        else: return "early_nosocomial"

# ---------- РАСЧЁТ ДОЗЫ ПРЕПАРАТА ----------
def calculate_drug_dose(drug_name, weight, crcl):
    doses_db = {
        "Цефтриаксон": {"std_dose": 2000, "interval": "1 р/сут", "unit": "мг", "renal_adjust": {30: 1000}},
        "Цефотаксим": {"std_dose": 2000, "interval": "3-4 р/сут", "unit": "мг", "renal_adjust": {20: (1000, "2-3 р/сут")}},
        "Цефтазидим": {"std_dose": 2000, "interval": "3 р/сут", "unit": "мг", "renal_adjust": {30: (1000, "2 р/сут"), 10: (1000, "1 р/сут")}},
        "Цефепим": {"std_dose": 2000, "interval": "2 р/сут", "unit": "мг", "renal_adjust": {60: (2000, "2 р/сут"), 30: (1000, "2 р/сут"), 10: (500, "1 р/сут")}},
        "Цефоперазон/сульбактам": {"std_dose": 2000, "interval": "2 р/сут", "unit": "мг", "renal_adjust": {}},
        "Меропенем": {"std_dose": 1000, "interval": "3 р/сут", "unit": "мг", "renal_adjust": {50: (1000, "2 р/сут"), 25: (500, "2 р/сут"), 10: (500, "1 р/сут")}},
        "Имипенем/циластатин": {"std_dose": 500, "interval": "4 р/сут", "unit": "мг", "renal_adjust": {50: (500, "3 р/сут"), 30: (500, "2 р/сут"), 10: (250, "1 р/сут")}},
        "Эртапенем": {"std_dose": 1000, "interval": "1 р/сут", "unit": "мг", "renal_adjust": {30: 500}},
        "Амоксициллин/клавуланат": {"std_dose": 1200, "interval": "3 р/сут", "unit": "мг", "renal_adjust": {30: (600, "2 р/сут"), 10: (600, "1 р/сут")}},
        "Ампициллин/сульбактам": {"std_dose": 1500, "interval": "4 р/сут", "unit": "мг", "renal_adjust": {30: (1500, "3 р/сут"), 10: (750, "2 р/сут")}},
        "Пиперациллин/тазобактам": {"std_dose": 4500, "interval": "3 р/сут", "unit": "мг", "renal_adjust": {40: (4500, "2 р/сут"), 20: (4500, "1 р/сут")}},
        "Левофлоксацин": {"std_dose": 500, "interval": "1-2 р/сут", "unit": "мг", "renal_adjust": {50: (500, "1 р/сут"), 20: (250, "1 р/сут")}},
        "Ципрофлоксацин": {"std_dose": 400, "interval": "2 р/сут", "unit": "мг", "renal_adjust": {50: (400, "2 р/сут"), 30: (400, "2 р/сут"), 10: (200, "2 р/сут")}},
        "Моксифлоксацин": {"std_dose": 400, "interval": "1 р/сут", "unit": "мг", "renal_adjust": {}},
        "Амикацин": {"std_dose": 15, "interval": "1 р/сут", "unit": "мг/кг", "is_weight_based": True, "renal_adjust": "удлинить интервал при CrCl<60"},
        "Гентамицин": {"std_dose": 1.2, "interval": "3 р/сут", "unit": "мг/кг", "is_weight_based": True, "renal_adjust": "по CrCl с мониторингом"},
        "Тобрамицин": {"std_dose": 5, "interval": "1 р/сут", "unit": "мг/кг", "is_weight_based": True, "renal_adjust": "удлинить интервал при CrCl<60"},
        "Ванкомицин": {"std_dose": 15, "interval": "каждые 8-12 ч", "unit": "мг/кг", "is_weight_based": True, "renal_adjust": "по CrCl с мониторингом"},
        "Тейкопланин": {"std_dose": 6, "interval": "1-2 р/сут", "unit": "мг/кг", "is_weight_based": True, "renal_adjust": "по CrCl"},
        "Линезолид": {"std_dose": 600, "interval": "2 р/сут", "unit": "мг", "renal_adjust": {}},
        "Даптомицин": {"std_dose": 6, "interval": "1 р/сут", "unit": "мг/кг", "is_weight_based": True, "renal_adjust": "увеличить интервал при CrCl<30"},
        "Тигециклин": {"std_dose": 100, "interval": "1 р/сут", "unit": "мг", "renal_adjust": {}},
        "Азитромицин": {"std_dose": 500, "interval": "1 р/сут", "unit": "мг", "renal_adjust": {}},
        "Кларитромицин": {"std_dose": 500, "interval": "2 р/сут", "unit": "мг", "renal_adjust": {}},
        "Доксициклин": {"std_dose": 100, "interval": "2 р/сут", "unit": "мг", "renal_adjust": {}},
        "Метронидазол": {"std_dose": 500, "interval": "3 р/сут", "unit": "мг", "renal_adjust": {}},
        "Клиндамицин": {"std_dose": 600, "interval": "3 р/сут", "unit": "мг", "renal_adjust": {}},
        "Колистин": {"std_dose": 2000000, "interval": "3 р/сут", "unit": "ЕД", "renal_adjust": "по CrCl"},
        "Фосфомицин": {"std_dose": 4000, "interval": "1 р/сут", "unit": "мг", "renal_adjust": {}},
        "Рифампицин": {"std_dose": 600, "interval": "1 р/сут", "unit": "мг", "renal_adjust": {}},
        "Котримоксазол": {"std_dose": 960, "interval": "2 р/сут", "unit": "мг", "renal_adjust": {30: (480, "2 р/сут"), 15: (480, "1 р/сут")}},
        "Пенициллин G": {"std_dose": 4000000, "interval": "4 р/сут", "unit": "ЕД", "renal_adjust": {10: (2000000, "4 р/сут")}},
        "Оксациллин": {"std_dose": 2000, "interval": "4 р/сут", "unit": "мг", "renal_adjust": {}},
        "Ампициллин": {"std_dose": 2000, "interval": "4 р/сут", "unit": "мг", "renal_adjust": {10: (1000, "4 р/сут")}},
    }
    if drug_name not in doses_db:
        return f"{drug_name}: дозу уточните по инструкции"
    info = doses_db[drug_name]
    std_dose = info["std_dose"]
    interval = info["interval"]
    unit = info.get("unit", "мг")
    is_weight_based = info.get("is_weight_based", False)
    if is_weight_based:
        dose_value = std_dose * weight
        dose_str = f"{dose_value:.0f} {unit}" if unit == "мг" else f"{dose_value:.0f} {unit}"
    else:
        dose_value = std_dose
        dose_str = f"{dose_value} {unit}"
    renal_adj = info.get("renal_adjust")
    if isinstance(renal_adj, dict) and renal_adj:
        for threshold, adj in sorted(renal_adj.items(), reverse=True):
            if crcl < threshold:
                if isinstance(adj, tuple):
                    adj_dose, adj_interval = adj
                    if is_weight_based:
                        adj_dose = adj_dose * weight
                        dose_str = f"{adj_dose:.0f} {unit}"
                    else:
                        dose_str = f"{adj_dose} {unit}"
                    interval = adj_interval
                else:
                    if is_weight_based:
                        adj_dose = adj * weight
                        dose_str = f"{adj_dose:.0f} {unit}"
                    else:
                        dose_str = f"{adj} {unit}"
                break
    elif isinstance(renal_adj, str):
        dose_str += f" (коррекция: {renal_adj})"
    return f"{dose_str} {interval}"

# ---------- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ (ПРОТОКОЛЫ) ----------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS protocols (
        id INTEGER PRIMARY KEY,
        localization TEXT,
        risk_level TEXT,
        line1 TEXT, line1_dose TEXT, line1_duration TEXT,
        line2 TEXT, line2_dose TEXT, line2_duration TEXT,
        line3 TEXT, line3_dose TEXT, line3_duration TEXT,
        allergy_alt TEXT, allergy_alt_dose TEXT, allergy_alt_duration TEXT,
        renal_alt TEXT, renal_alt_dose TEXT, renal_alt_duration TEXT,
        note TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS risk_factors (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS pathogens (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        sensitive_drugs TEXT
    )''')
    cur.execute("SELECT COUNT(*) FROM protocols")
    if cur.fetchone()[0] == 0:
        protocols = [
            ("Пневмония", "community", "Амоксициллин/клавуланат","1.2 г в/в 3 р/сут","7-10 дней","Цефтриаксон+Азитромицин","Цефтриаксон 2 г 1 р/сут + Азитромицин 500 мг 1 р/сут","7-10 дней","Левофлоксацин","500 мг в/в 1-2 р/сут","7-10 дней","Макролиды (кларитромицин)","500 мг внутрь 2 р/сут","7-10 дней","Цефтриаксон 1 г/сут при CrCl<30","1 г/сут","7-10 дней","Внебольничная пневмония"),
            ("Пневмония","early_nosocomial","Цефтриаксон","2 г в/в 1 р/сут","7-14 дней","Левофлоксацин","500 мг в/в 2 р/сут","7-14 дней","Моксифлоксацин","400 мг в/в 1 р/сут","7-14 дней","Азитромицин+Цефтриаксон","Азитромицин 500 мг + Цефтриаксон 2 г","7-14 дней","Цефтриаксон 1 г/сут при CrCl<30","1 г/сут","7-14 дней","Ранняя нозокомиальная"),
            ("Пневмония","late_mrsa","Линезолид+Цефепим","Линезолид 600 мг 2 р/сут + Цефепим 2 г 2 р/сут","10-14 дней","Ванкомицин+Пиперациллин/тазобактам","Ванкомицин 15-20 мг/кг + Пиперациллин/тазобактам 4.5 г 3 р/сут","10-14 дней","Тигециклин+Цефепим","Тигециклин 100 мг + Цефепим 2 г","10-14 дней","Линезолид","600 мг 2 р/сут","10-14 дней","Коррекция цефепима при CrCl<60","1-2 г 1 р/сут","10-14 дней","Риск МРЗС"),
            ("Пневмония","late_pseudomonas","Пиперациллин/тазобактам","4.5 г в/в 3-4 р/сут","10-14 дней","Меропенем+Амикацин","Меропенем 1 г 3 р/сут + Амикацин 15-20 мг/кг 1 р/сут","10-14 дней","Цефепим+Амикацин","Цефепим 2 г 2 р/сут + Амикацин","10-14 дней","Колистин+Меропенем","Колистин 2-3 млн ЕД + Меропенем","10-14 дней","Коррекция всех по CrCl","по инструкции","10-14 дней","Риск P. aeruginosa"),
            ("Интраабдоминальная","community","Цефтриаксон+Метронидазол","Цефтриаксон 2 г + Метронидазол 500 мг 3 р/сут","5-7 дней","Левофлоксацин+Метронидазол","Левофлоксацин 500 мг + Метронидазол","5-7 дней","Моксифлоксацин","400 мг в/в 1 р/сут","5-7 дней","Метронидазол+амоксициллин/клавуланат","амоксициллин/клавуланат 1.2 г + Метронидазол","5-7 дней","Метронидазол без коррекции","500 мг 3 р/сут","5-7 дней","Внебольничная"),
            ("Интраабдоминальная","late_mrsa","Ванкомицин+Пиперациллин/тазобактам","Ванкомицин 15-20 мг/кг + Пиперациллин/тазобактам 4.5 г 3 р/сут","7-14 дней","Тигециклин","100 мг в/в 1 р/сут","7-14 дней","Линезолид+Меропенем","Линезолид 600 мг + Меропенем 1 г","7-14 дней","Даптомицин+Метронидазол","Даптомицин 6 мг/кг + Метронидазол","7-14 дней","Коррекция ванкомицина и пиперациллина","по CrCl","7-14 дней","Риск МРЗС"),
            ("Интраабдоминальная","late_pseudomonas","Меропенем","1 г в/в 3 р/сут","7-14 дней","Пиперациллин/тазобактам+Амикацин","4.5 г + Амикацин 15 мг/кг","7-14 дней","Цефепим+Метронидазол","Цефепим 2 г + Метронидазол 500 мг","7-14 дней","Колистин+Меропенем","Колистин 2-3 млн ЕД + Меропенем","7-14 дней","Коррекция по CrCl","по инструкции","7-14 дней","Риск P. aeruginosa"),
            ("ИМВП","community","Цефтриаксон","2 г в/в 1 р/сут","5-7 дней","Левофлоксацин","500 мг в/в 1-2 р/сут","5-7 дней","Ципрофлоксацин","500 мг в/в 2 р/сут","5-7 дней","Нитрофурантоин","100 мг внутрь 3 р/сут","5 дней","Цефтриаксон 1 г/сут при CrCl<30","1 г/сут","5-7 дней","Внебольничная ИМВП"),
            ("ИМВП","late_mrsa","Ванкомицин","15-20 мг/кг 2-3 р/сут","7-10 дней","Линезолид","600 мг в/в 2 р/сут","7-10 дней","Даптомицин","6 мг/кг 1 р/сут","7-10 дней","Рифампицин+котримоксазол","по инструкции","7-10 дней","Ванкомицин по CrCl","по инструкции","7-10 дней","Катетер-ассоциированная, риск МРЗС"),
            ("ИМВП","late_pseudomonas","Пиперациллин/тазобактам","4.5 г в/в 3 р/сут","7-10 дней","Цефепим","2 г в/в 2 р/сут","7-10 дней","Меропенем","1 г в/в 3 р/сут","7-10 дней","Амикацин","15 мг/кг 1 р/сут","7-10 дней","Коррекция всех","по CrCl","7-10 дней","Риск P. aeruginosa"),
            ("Сепсис","early_nosocomial","Пиперациллин/тазобактам","4.5 г в/в 3-4 р/сут","7-14 дней","Цефтриаксон+Метронидазол","Цефтриаксон 2 г + Метронидазол 500 мг","7-14 дней","Меропенем","1 г в/в 3 р/сут","7-14 дней","Ванкомицин+Цефтриаксон","Ванкомицин 15 мг/кг + Цефтриаксон 2 г","7-14 дней","Коррекция по CrCl","по инструкции","7-14 дней","Ранний нозокомиальный сепсис"),
            ("Сепсис","late_mrsa","Ванкомицин+Пиперациллин/тазобактам","Ванкомицин 15-20 мг/кг + Пиперациллин/тазобактам 4.5 г 3 р/сут","7-14 дней","Меропенем+Линезолид","Меропенем 1 г + Линезолид 600 мг","7-14 дней","Цефепим+Линезолид+Метронидазол","Цефепим 2 г + Линезолид + Метронидазол","7-14 дней","Даптомицин+Меропенем","Даптомицин 6 мг/кг + Меропенем","7-14 дней","Коррекция всех","по CrCl","7-14 дней","Риск МРЗС"),
            ("Сепсис","late_pseudomonas","Меропенем+Амикацин","Меропенем 1 г 3 р/сут + Амикацин 15 мг/кг 1 р/сут","7-14 дней","Цефепим+Амикацин","Цефепим 2 г + Амикацин","7-14 дней","Пиперациллин/тазобактам+Тобрамицин","4.5 г + Тобрамицин 5 мг/кг","7-14 дней","Колистин+Меропенем","Колистин 2-3 млн ЕД + Меропенем","7-14 дней","Коррекция всех","по CrCl","7-14 дней","Риск P. aeruginosa"),
            ("Инфекция кожи и мягких тканей","community","Цефтриаксон","2 г в/в 1 р/сут","7-10 дней","Амоксициллин/клавуланат","1.2 г в/в 3 р/сут","7-10 дней","Левофлоксацин","500 мг в/в 1-2 р/сут","7-10 дней","Клиндамицин","600 мг в/в 3 р/сут","7-10 дней","Коррекция по CrCl","по инструкции","7-10 дней","Целлюлит, рожа, абсцесс"),
            ("Инфекция кожи и мягких тканей","late_mrsa","Ванкомицин","15-20 мг/кг 2-3 р/сут","7-14 дней","Линезолид","600 мг в/в 2 р/сут","7-14 дней","Даптомицин","6 мг/кг 1 р/сут","7-14 дней","Тигециклин","100 мг в/в 1 р/сут","7-14 дней","Ванкомицин по CrCl","по инструкции","7-14 дней","Риск МРЗС"),
            ("Инфекция кожи и мягких тканей","late_pseudomonas","Пиперациллин/тазобактам","4.5 г в/в 3 р/сут","10-14 дней","Цефепим","2 г в/в 2 р/сут","10-14 дней","Меропенем","1 г в/в 3 р/сут","10-14 дней","Амикацин","15 мг/кг 1 р/сут","10-14 дней","Коррекция всех","по CrCl","10-14 дней","Диабетическая стопа, инфицированная язва"),
            ("Менингит","community","Цефтриаксон","2 г в/в 2 р/сут","10-14 дней","Меропенем","2 г в/в 3 р/сут","10-14 дней","Пенициллин G","4 млн ЕД в/в 4 р/сут","10-14 дней","Ванкомицин+Цефтриаксон","Ванкомицин 15 мг/кг + Цефтриаксон 2 г","10-14 дней","Коррекция цефтриаксона при CrCl<30","1 г 2 р/сут","10-14 дней","Бактериальный менингит"),
            ("Менингит","nosocomial","Меропенем","2 г в/в 3 р/сут","14-21 день","Цефепим+Ванкомицин","Цефепим 2 г + Ванкомицин 15 мг/кг","14-21 день","Цефтазидим+Ванкомицин","Цефтазидим 2 г + Ванкомицин","14-21 день","Линезолид+Меропенем","Линезолид 600 мг + Меропенем","14-21 день","Коррекция всех по CrCl","по инструкции","14-21 день","Нозокомиальный менингит"),
            ("Эндокардит","community","Ампициллин+Гентамицин","Ампициллин 2 г 4 р/сут + Гентамицин 1 мг/кг 3 р/сут","4-6 недель","Ванкомицин+Гентамицин","Ванкомицин 15 мг/кг + Гентамицин 1 мг/кг","4-6 недель","Цефтриаксон","2 г в/в 1 р/сут","4-6 недель","Даптомицин","6-8 мг/кг 1 р/сут","4-6 недель","Коррекция гентамицина по CrCl","по инструкции","4-6 недель","Эндокардит нативных клапанов"),
            ("Эндокардит","late_mrsa","Ванкомицин+Гентамицин","Ванкомицин 15 мг/кг + Гентамицин 1 мг/кг 3 р/сут","6 недель","Даптомицин+Гентамицин","Даптомицин 8 мг/кг + Гентамицин 1 мг/кг","6 недель","Линезолид","600 мг в/в 2 р/сут","6 недель","Ампициллин+Ванкомицин","Ампициллин 2 г + Ванкомицин","6 недель","Коррекция всех","по CrCl","6 недель","Эндокардит протезированных клапанов"),
            ("Остеомиелит","community","Цефтриаксон","2 г в/в 1 р/сут","4-6 недель","Клиндамицин","600 мг в/в 3 р/сут","4-6 недель","Левофлоксацин","500 мг в/в 1-2 р/сут","4-6 недель","Амоксициллин/клавуланат","1.2 г в/в 3 р/сут","4-6 недель","Коррекция по CrCl","по инструкции","4-6 недель","Гематогенный остеомиелит"),
            ("Остеомиелит","late_mrsa","Ванкомицин","15-20 мг/кг 2-3 р/сут","6 недель","Линезолид","600 мг в/в 2 р/сут","6 недель","Даптомицин","6-8 мг/кг 1 р/сут","6 недель","Тигециклин","100 мг в/в 1 р/сут","6 недель","Ванкомицин по CrCl","по инструкции","6 недель","Хронический остеомиелит, импланты"),
            ("Остеомиелит","late_pseudomonas","Пиперациллин/тазобактам","4.5 г в/в 3 р/сут","6-8 недель","Цефепим+Ципрофлоксацин","Цефепим 2 г + Ципрофлоксацин 400 мг 2 р/сут","6-8 недель","Меропенем","1 г в/в 3 р/сут","6-8 недель","Амикацин+Цефтазидим","Амикацин 15 мг/кг + Цефтазидим 2 г","6-8 недель","Коррекция всех","по CrCl","6-8 недель","Остеомиелит с риском P. aeruginosa"),
        ]
        cur.executemany('''INSERT INTO protocols (localization, risk_level,
            line1, line1_dose, line1_duration,
            line2, line2_dose, line2_duration,
            line3, line3_dose, line3_duration,
            allergy_alt, allergy_alt_dose, allergy_alt_duration,
            renal_alt, renal_alt_dose, renal_alt_duration,
            note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', protocols)
    cur.execute("SELECT COUNT(*) FROM risk_factors")
    if cur.fetchone()[0] == 0:
        factors = ["ИВЛ > 5 дней","Предшествующие антибиотики (цефалоспорины/фторхинолоны)","Колонизация/инфекция МРЗС в анамнезе","Нейтропения (<500)","Катетер центральной вены >7 дней","Послеоперационная рана (абдоминальная)","Длительная госпитализация (>14 дней)"]
        for f in factors: cur.execute("INSERT INTO risk_factors (name) VALUES (?)", (f,))
    cur.execute("SELECT COUNT(*) FROM pathogens")
    if cur.fetchone()[0] == 0:
        pathogens_data = [
            ("E. coli", json.dumps(["Цефтриаксон", "Амоксициллин/клавуланат", "Меропенем", "Амикацин", "Ципрофлоксацин", "Левофлоксацин"])),
            ("K. pneumoniae", json.dumps(["Меропенем", "Амикацин", "Цефепим", "Ципрофлоксацин", "Левофлоксацин"])),
            ("P. aeruginosa", json.dumps(["Пиперациллин/тазобактам", "Меропенем", "Цефепим", "Амикацин", "Ципрофлоксацин", "Тобрамицин"])),
            ("S. aureus (MSSA)", json.dumps(["Оксациллин", "Цефтриаксон", "Клиндамицин", "Ванкомицин", "Линезолид"])),
            ("S. aureus (MRSA)", json.dumps(["Ванкомицин", "Линезолид", "Даптомицин", "Тигециклин"])),
            ("S. pyogenes", json.dumps(["Пенициллин G", "Цефтриаксон", "Клиндамицин", "Эритромицин"])),
            ("N. meningitidis", json.dumps(["Цефтриаксон", "Пенициллин G", "Ципрофлоксацин", "Меропенем"])),
            ("Enterococcus faecalis", json.dumps(["Ампициллин", "Ванкомицин", "Линезолид", "Даптомицин"])),
            ("Enterococcus faecium (VRE)", json.dumps(["Линезолид", "Даптомицин", "Тигециклин"])),
            ("Proteus mirabilis", json.dumps(["Цефтриаксон", "Левофлоксацин", "Ципрофлоксацин", "Меропенем"])),
            ("Enterobacter cloacae", json.dumps(["Меропенем", "Цефепим", "Амикацин", "Ципрофлоксацин"])),
            ("Acinetobacter baumannii", json.dumps(["Колистин", "Тигециклин", "Меропенем (если чувствителен)", "Амикацин"])),
            ("Bacteroides fragilis", json.dumps(["Метронидазол", "Клиндамицин", "Пиперациллин/тазобактам", "Меропенем"])),
            ("Legionella pneumophila", json.dumps(["Азитромицин", "Левофлоксацин", "Моксифлоксацин"])),
            ("Mycoplasma pneumoniae", json.dumps(["Азитромицин", "Доксициклин", "Левофлоксацин"])),
            ("Chlamydia pneumoniae", json.dumps(["Азитромицин", "Доксициклин", "Левофлоксацин"])),
        ]
        cur.executemany("INSERT INTO pathogens (name, sensitive_drugs) VALUES (?,?)", pathogens_data)
    conn.commit()
    conn.close()

def get_protocol(localization, risk_level):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('''SELECT line1, line1_dose, line1_duration,
                          line2, line2_dose, line2_duration,
                          line3, line3_dose, line3_duration,
                          allergy_alt, allergy_alt_dose, allergy_alt_duration,
                          renal_alt, renal_alt_dose, renal_alt_duration,
                          note
                   FROM protocols WHERE localization=? AND risk_level=?''', (localization, risk_level))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "line1": row[0], "line1_dose": row[1], "line1_duration": row[2],
            "line2": row[3], "line2_dose": row[4], "line2_duration": row[5],
            "line3": row[6], "line3_dose": row[7], "line3_duration": row[8],
            "allergy_alt": row[9], "allergy_alt_dose": row[10], "allergy_alt_duration": row[11],
            "renal_alt": row[12], "renal_alt_dose": row[13], "renal_alt_duration": row[14],
            "note": row[15]
        }
    return None

def get_pathogen_sensitivity(pathogen_name):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT sensitive_drugs FROM pathogens WHERE name=?", (pathogen_name,))
    row = cur.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return []

# ---------- FLASK РОУТЫ ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/risk_factors', methods=['GET'])
def get_risk_factors():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT name FROM risk_factors")
    factors = [row[0] for row in cur.fetchall()]
    conn.close()
    return jsonify(factors)

@app.route('/api/pathogens', methods=['GET'])
def get_pathogens():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT name FROM pathogens")
    pathogens = [row[0] for row in cur.fetchall()]
    conn.close()
    return jsonify(pathogens)

ALL_ANTIBIOTICS = [
    "Цефтриаксон", "Цефотаксим", "Цефтазидим", "Цефепим", "Цефоперазон/сульбактам",
    "Меропенем", "Имипенем/циластатин", "Эртапенем",
    "Амоксициллин/клавуланат", "Ампициллин/сульбактам", "Пиперациллин/тазобактам",
    "Левофлоксацин", "Ципрофлоксацин", "Моксифлоксацин",
    "Амикацин", "Гентамицин", "Тобрамицин",
    "Ванкомицин", "Тейкопланин", "Линезолид", "Даптомицин", "Тигециклин",
    "Азитромицин", "Кларитромицин", "Доксициклин", "Метронидазол", "Клиндамицин",
    "Колистин", "Фосфомицин", "Рифампицин", "Котримоксазол", "Пенициллин G", "Оксациллин", "Ампициллин"
]

@app.route('/api/all_antibiotics', methods=['GET'])
def all_antibiotics():
    return jsonify(ALL_ANTIBIOTICS)

@app.route('/api/pathogen_sensitivity', methods=['GET'])
def pathogen_sensitivity():
    name = request.args.get('name')
    sens = get_pathogen_sensitivity(name)
    return jsonify(sens)

@app.route('/api/empiric', methods=['POST'])
def empiric():
    data = request.json
    localization = data.get('localization')
    hospital_days = float(data.get('hospital_days', 1))
    risk_factors = data.get('risk_factors', [])
    age = int(data.get('age', 60))
    weight = float(data.get('weight', 70))
    creatinine = float(data.get('creatinine', 80))
    sex = data.get('sex', 'male')
    sofa = int(data.get('sofa', 0))
    leukocytes = data.get('leukocytes', '4.0-10.0')
    esr = data.get('esr', '10-30')
    pct = data.get('pct', '<0.1')
    allergy = data.get('allergy', 'нет')
    if not localization:
        return jsonify({"error": "Выберите локализацию"}), 400
    crcl = calculate_crcl(age, weight, creatinine, sex)
    risk_level = determine_risk_level(hospital_days, risk_factors)
    protocol = get_protocol(localization, risk_level)
    if not protocol:
        return jsonify({"error": f"Нет протокола для {localization} и уровня {risk_level}"}), 404
    lab = interpret_lab(leukocytes, esr, pct)
    sofa_text = interpret_sofa(sofa)
    def calc_for_line(line_text):
        if not line_text or line_text == "—":
            return ""
        drugs = [d.strip() for d in re.split(r'[+,]', line_text) if d.strip()]
        doses = []
        for drug in drugs:
            dose_str = calculate_drug_dose(drug, weight, crcl)
            doses.append(f"{drug}: {dose_str}")
        return "; ".join(doses)
    return jsonify({
        "stratification": risk_level,
        "crcl": round(crcl, 1),
        "sofa_text": sofa_text,
        "lab": lab,
        "line1": protocol["line1"], "line1_dose": calc_for_line(protocol["line1"]), "line1_duration": protocol["line1_duration"],
        "line2": protocol["line2"], "line2_dose": calc_for_line(protocol["line2"]), "line2_duration": protocol["line2_duration"],
        "line3": protocol["line3"], "line3_dose": calc_for_line(protocol["line3"]), "line3_duration": protocol["line3_duration"],
        "allergy_alt": protocol["allergy_alt"], "allergy_alt_dose": calc_for_line(protocol["allergy_alt"]), "allergy_alt_duration": protocol["allergy_alt_duration"],
        "renal_alt": protocol["renal_alt"], "renal_alt_dose": calc_for_line(protocol["renal_alt"]), "renal_alt_duration": protocol["renal_alt_duration"],
        "note": protocol["note"],
        "allergy_warning": f"Аллергия на {allergy}" if allergy != "нет" else None
    })

@app.route('/api/targeted', methods=['POST'])
def targeted():
    data = request.json
    pathogen = data.get('pathogen')
    resistance_drugs = data.get('resistance_drugs', [])
    age = int(data.get('age', 60))
    weight = float(data.get('weight', 70))
    creatinine = float(data.get('creatinine', 80))
    sex = data.get('sex', 'male')
    previous_antibiotics = data.get('previous_antibiotics', '')
    hospital_days = float(data.get('hospital_days', 1))
    allergy = data.get('allergy', 'нет')
    if not pathogen:
        return jsonify({"error": "Выберите возбудителя"}), 400
    crcl = calculate_crcl(age, weight, creatinine, sex)
    sensitive_all = get_pathogen_sensitivity(pathogen)
    recommended = [drug for drug in sensitive_all if drug not in resistance_drugs]
    if not recommended:
        return jsonify({"error": "Нет доступных препаратов с учётом резистентности. Требуется консультация микробиолога."}), 404
    result = []
    for drug in recommended[:5]:
        dose_str = calculate_drug_dose(drug, weight, crcl)
        result.append({"drug": drug, "dose": dose_str, "note": "Длительность зависит от локализации и клинического ответа (обычно 7-14 дней)"})
    return jsonify({
        "pathogen": pathogen,
        "crcl": round(crcl, 1),
        "recommendations": result,
        "previous_antibiotics": previous_antibiotics,
        "hospital_days": hospital_days,
        "allergy_warning": f"Аллергия на {allergy}" if allergy != "нет" else None
    })

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8080, debug=False)
