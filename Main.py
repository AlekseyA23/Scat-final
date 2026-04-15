from flask import Flask, render_template, request, jsonify
import sqlite3
import json

app = Flask(__name__)
DB_NAME = "skat_bot.db"

# ---------- РАСЧЁТ СКФ И КОРРЕКЦИЯ ДОЗ ----------
def calculate_crcl(age, weight, creatinine, sex):
    if creatinine > 20:
        scr_mgdl = creatinine / 88.4
    else:
        scr_mgdl = creatinine
    crcl = (140 - age) * weight / (72 * scr_mgdl)
    if sex == 'female':
        crcl *= 0.85
    return max(crcl, 5.0)

def get_dose_adjustment(drug_name, crcl):
    adjustments = {
        "Цефтриаксон": f"CrCl {crcl:.0f} мл/мин: " + ("1 г/сут" if crcl < 30 else "2 г/сут"),
        "Меропенем": "CrCl 26-50: 1 г 2 р/сут; 10-25: 0.5 г 2 р/сут; <10: 0.5 г 1 р/сут",
        "Пиперациллин/тазобактам": "CrCl 20-40: 4.5 г 2 р/сут; <20: 4.5 г 1 р/сут",
        "Ванкомицин": "Коррекция по CrCl с мониторингом уровня",
        "Амикацин": "При CrCl <60 удлинить интервал до 36-48 ч",
        "Цефепим": "CrCl 30-60: 1-2 г 2 р/сут; 11-29: 1-2 г 1 р/сут; <10: 0.5-1 г 1 р/сут",
        "Левофлоксацин": "CrCl 20-49: 250 мг 1-2 р/сут; <20: 250 мг 1 р/сут",
        "Ципрофлоксацин": "CrCl 30-50: 200-400 мг 2 р/сут; CrCl <30: 200 мг 2 р/сут",
        "Линезолид": "Коррекции не требуется",
        "Метронидазол": "Коррекции не требуется",
        "Клиндамицин": "Коррекции не требуется",
        "Даптомицин": "Коррекция по CrCl",
        "Тигециклин": "Коррекции не требуется",
        "Азитромицин": "Коррекции не требуется",
        "Моксифлоксацин": "Коррекции не требуется",
        "Доксициклин": "Коррекции не требуется",
        "Эртапенем": "CrCl <30: 500 мг/сут",
        "Ампициллин": "Коррекция при CrCl <10: 1-2 г 1 р/сут",
        "Гентамицин": "Коррекция по CrCl с мониторингом",
        "Пенициллин G": "Коррекция при CrCl <10: 2-3 млн ЕД 2 р/сут",
        "Цефтазидим": "CrCl 10-30: 1 г 2 р/сут; <10: 1 г 1 р/сут",
        "Тобрамицин": "Коррекция по CrCl",
        "Рифампицин": "Коррекции не требуется",
        "Котримоксазол": "Коррекция по CrCl",
    }
    return adjustments.get(drug_name, "Коррекция не описана")

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

# ---------- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ----------
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
    
    # Заполнение протоколов (расширенные данные)
    cur.execute("SELECT COUNT(*) FROM protocols")
    if cur.fetchone()[0] == 0:
        protocols = [
            # ---------- Пневмония ----------
            ("Пневмония", "community", "Амоксициллин/клавуланат","1.2 г в/в 3 р/сут","7-10 дней","Цефтриаксон+Азитромицин","Цефтриаксон 2 г 1 р/сут + Азитромицин 500 мг 1 р/сут","7-10 дней","Левофлоксацин","500 мг в/в 1-2 р/сут","7-10 дней","Макролиды (кларитромицин)","500 мг внутрь 2 р/сут","7-10 дней","Цефтриаксон 1 г/сут при CrCl<30","1 г/сут","7-10 дней","Внебольничная пневмония"),
            ("Пневмония","early_nosocomial","Цефтриаксон","2 г в/в 1 р/сут","7-14 дней","Левофлоксацин","500 мг в/в 2 р/сут","7-14 дней","Моксифлоксацин","400 мг в/в 1 р/сут","7-14 дней","Азитромицин+Цефтриаксон","Азитромицин 500 мг + Цефтриаксон 2 г","7-14 дней","Цефтриаксон 1 г/сут при CrCl<30","1 г/сут","7-14 дней","Ранняя нозокомиальная"),
            ("Пневмония","late_mrsa","Линезолид+Цефепим","Линезолид 600 мг 2 р/сут + Цефепим 2 г 2 р/сут","10-14 дней","Ванкомицин+Пиперациллин/тазобактам","Ванкомицин 15-20 мг/кг + Пиперациллин/тазобактам 4.5 г 3 р/сут","10-14 дней","Тигециклин+Цефепим","Тигециклин 100 мг + Цефепим 2 г","10-14 дней","Линезолид","600 мг 2 р/сут","10-14 дней","Коррекция цефепима при CrCl<60","1-2 г 1 р/сут","10-14 дней","Риск МРЗС"),
            ("Пневмония","late_pseudomonas","Пиперациллин/тазобактам","4.5 г в/в 3-4 р/сут","10-14 дней","Меропенем+Амикацин","Меропенем 1 г 3 р/сут + Амикацин 15-20 мг/кг 1 р/сут","10-14 дней","Цефепим+Амикацин","Цефепим 2 г 2 р/сут + Амикацин","10-14 дней","Колистин+Меропенем","Колистин 2-3 млн ЕД + Меропенем","10-14 дней","Коррекция всех по CrCl","по инструкции","10-14 дней","Риск P. aeruginosa"),
            # ---------- Интраабдоминальная ----------
            ("Интраабдоминальная","community","Цефтриаксон+Метронидазол","Цефтриаксон 2 г + Метронидазол 500 мг 3 р/сут","5-7 дней","Левофлоксацин+Метронидазол","Левофлоксацин 500 мг + Метронидазол","5-7 дней","Моксифлоксацин","400 мг в/в 1 р/сут","5-7 дней","Метронидазол+амоксициллин/клавуланат","амоксициллин/клавуланат 1.2 г + Метронидазол","5-7 дней","Метронидазол без коррекции","500 мг 3 р/сут","5-7 дней","Внебольничная"),
            ("Интраабдоминальная","late_mrsa","Ванкомицин+Пиперациллин/тазобактам","Ванкомицин 15-20 мг/кг + Пиперациллин/тазобактам 4.5 г 3 р/сут","7-14 дней","Тигециклин","100 мг в/в 1 р/сут","7-14 дней","Линезолид+Меропенем","Линезолид 600 мг + Меропенем 1 г","7-14 дней","Даптомицин+Метронидазол","Даптомицин 6 мг/кг + Метронидазол","7-14 дней","Коррекция ванкомицина и пиперациллина","по CrCl","7-14 дней","Риск МРЗС"),
            ("Интраабдоминальная","late_pseudomonas","Меропенем","1 г в/в 3 р/сут","7-14 дней","Пиперациллин/тазобактам+Амикацин","4.5 г + Амикацин 15 мг/кг","7-14 дней","Цефепим+Метронидазол","Цефепим 2 г + Метронидазол 500 мг","7-14 дней","Колистин+Меропенем","Колистин 2-3 млн ЕД + Меропенем","7-14 дней","Коррекция по CrCl","по инструкции","7-14 дней","Риск P. aeruginosa"),
            # ---------- ИМВП ----------
            ("ИМВП","community","Цефтриаксон","2 г в/в 1 р/сут","5-7 дней","Левофлоксацин","500 мг в/в 1-2 р/сут","5-7 дней","Ципрофлоксацин","500 мг в/в 2 р/сут","5-7 дней","Нитрофурантоин","100 мг внутрь 3 р/сут","5 дней","Цефтриаксон 1 г/сут при CrCl<30","1 г/сут","5-7 дней","Внебольничная ИМВП"),
            ("ИМВП","late_mrsa","Ванкомицин","15-20 мг/кг 2-3 р/сут","7-10 дней","Линезолид","600 мг в/в 2 р/сут","7-10 дней","Даптомицин","6 мг/кг 1 р/сут","7-10 дней","Рифампицин+котримоксазол","по инструкции","7-10 дней","Ванкомицин по CrCl","по инструкции","7-10 дней","Катетер-ассоциированная, риск МРЗС"),
            ("ИМВП","late_pseudomonas","Пиперациллин/тазобактам","4.5 г в/в 3 р/сут","7-10 дней","Цефепим","2 г в/в 2 р/сут","7-10 дней","Меропенем","1 г в/в 3 р/сут","7-10 дней","Амикацин","15 мг/кг 1 р/сут","7-10 дней","Коррекция всех","по CrCl","7-10 дней","Риск P. aeruginosa"),
            # ---------- Сепсис ----------
            ("Сепсис","early_nosocomial","Пиперациллин/тазобактам","4.5 г в/в 3-4 р/сут","7-14 дней","Цефтриаксон+Метронидазол","Цефтриаксон 2 г + Метронидазол 500 мг","7-14 дней","Меропенем","1 г в/в 3 р/сут","7-14 дней","Ванкомицин+Цефтриаксон","Ванкомицин 15 мг/кг + Цефтриаксон 2 г","7-14 дней","Коррекция по CrCl","по инструкции","7-14 дней","Ранний нозокомиальный сепсис"),
            ("Сепсис","late_mrsa","Ванкомицин+Пиперациллин/тазобактам","Ванкомицин 15-20 мг/кг + Пиперациллин/тазобактам 4.5 г 3 р/сут","7-14 дней","Меропенем+Линезолид","Меропенем 1 г + Линезолид 600 мг","7-14 дней","Цефепим+Линезолид+Метронидазол","Цефепим 2 г + Линезолид + Метронидазол","7-14 дней","Даптомицин+Меропенем","Даптомицин 6 мг/кг + Меропенем","7-14 дней","Коррекция всех","по CrCl","7-14 дней","Риск МРЗС"),
            ("Сепсис","late_pseudomonas","Меропенем+Амикацин","Меропенем 1 г 3 р/сут + Амикацин 15 мг/кг 1 р/сут","7-14 дней","Цефепим+Амикацин","Цефепим 2 г + Амикацин","7-14 дней","Пиперациллин/тазобактам+Тобрамицин","4.5 г + Тобрамицин 5 мг/кг","7-14 дней","Колистин+Меропенем","Колистин 2-3 млн ЕД + Меропенем","7-14 дней","Коррекция всех","по CrCl","7-14 дней","Риск P. aeruginosa"),
            # ---------- ИНФЕКЦИЯ КОЖИ И МЯГКИХ ТКАНЕЙ ----------
            ("Инфекция кожи и мягких тканей","community","Цефтриаксон","2 г в/в 1 р/сут","7-10 дней","Амоксициллин/клавуланат","1.2 г в/в 3 р/сут","7-10 дней","Левофлоксацин","500 мг в/в 1-2 р/сут","7-10 дней","Клиндамицин","600 мг в/в 3 р/сут","7-10 дней","Коррекция по CrCl","по инструкции","7-10 дней","Целлюлит, рожа, абсцесс"),
            ("Инфекция кожи и мягких тканей","late_mrsa","Ванкомицин","15-20 мг/кг 2-3 р/сут","7-14 дней","Линезолид","600 мг в/в 2 р/сут","7-14 дней","Даптомицин","6 мг/кг 1 р/сут","7-14 дней","Тигециклин","100 мг в/в 1 р/сут","7-14 дней","Ванкомицин по CrCl","по инструкции","7-14 дней","Риск МРЗС"),
            ("Инфекция кожи и мягких тканей","late_pseudomonas","Пиперациллин/тазобактам","4.5 г в/в 3 р/сут","10-14 дней","Цефепим","2 г в/в 2 р/сут","10-14 дней","Меропенем","1 г в/в 3 р/сут","10-14 дней","Амикацин","15 мг/кг 1 р/сут","10-14 дней","Коррекция всех","по CrCl","10-14 дней","Диабетическая стопа, инфицированная язва"),
            # ---------- МЕНИНГИТ ----------
            ("Менингит","community","Цефтриаксон","2 г в/в 2 р/сут","10-14 дней","Меропенем","2 г в/в 3 р/сут","10-14 дней","Пенициллин G","4 млн ЕД в/в 4 р/сут","10-14 дней","Ванкомицин+Цефтриаксон","Ванкомицин 15 мг/кг + Цефтриаксон 2 г","10-14 дней","Коррекция цефтриаксона при CrCl<30","1 г 2 р/сут","10-14 дней","Бактериальный менингит (N. meningitidis, S. pneumoniae)"),
            ("Менингит","nosocomial","Меропенем","2 г в/в 3 р/сут","14-21 день","Цефепим+Ванкомицин","Цефепим 2 г + Ванкомицин 15 мг/кг","14-21 день","Цефтазидим+Ванкомицин","Цефтазидим 2 г + Ванкомицин","14-21 день","Линезолид+Меропенем","Линезолид 600 мг + Меропенем","14-21 день","Коррекция всех по CrCl","по инструкции","14-21 день","Нозокомиальный менингит (внутрижелудочковые катетеры)"),
            # ---------- ЭНДОКАРДИТ ----------
            ("Эндокардит","community","Ампициллин+Гентамицин","Ампициллин 2 г в/в 4 р/сут + Гентамицин 1 мг/кг в/в 3 р/сут","4-6 недель","Ванкомицин+Гентамицин","Ванкомицин 15 мг/кг + Гентамицин 1 мг/кг","4-6 недель","Цефтриаксон","2 г в/в 1 р/сут","4-6 недель","Даптомицин","6-8 мг/кг в/в 1 р/сут","4-6 недель","Коррекция гентамицина по CrCl","по инструкции","4-6 недель","Эндокардит нативных клапанов (Enterococcus, Streptococcus)"),
            ("Эндокардит","late_mrsa","Ванкомицин+Гентамицин","Ванкомицин 15 мг/кг + Гентамицин 1 мг/кг 3 р/сут","6 недель","Даптомицин+Гентамицин","Даптомицин 8 мг/кг + Гентамицин 1 мг/кг","6 недель","Линезолид","600 мг в/в 2 р/сут","6 недель","Ампициллин+Ванкомицин","Ампициллин 2 г + Ванкомицин","6 недель","Коррекция всех","по CrCl","6 недель","Эндокардит протезированных клапанов, риск МРЗС"),
            # ---------- ОСТЕОМИЕЛИТ ----------
            ("Остеомиелит","community","Цефтриаксон","2 г в/в 1 р/сут","4-6 недель","Клиндамицин","600 мг в/в 3 р/сут","4-6 недель","Левофлоксацин","500 мг в/в 1-2 р/сут","4-6 недель","Амоксициллин/клавуланат","1.2 г в/в 3 р/сут","4-6 недель","Коррекция по CrCl","по инструкции","4-6 недель","Гематогенный остеомиелит, диабетическая стопа"),
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
    
    # Факторы риска
    cur.execute("SELECT COUNT(*) FROM risk_factors")
    if cur.fetchone()[0] == 0:
        factors = [
            "ИВЛ > 5 дней",
            "Предшествующие антибиотики (цефалоспорины/фторхинолоны)",
            "Колонизация/инфекция МРЗС в анамнезе",
            "Нейтропения (<500)",
            "Катетер центральной вены >7 дней",
            "Послеоперационная рана (абдоминальная)",
            "Длительная госпитализация (>14 дней)"
        ]
        for f in factors:
            cur.execute("INSERT INTO risk_factors (name) VALUES (?)", (f,))
    
    # Возбудители (расширенный спектр)
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
            ("Streptococcus viridans", json.dumps(["Пенициллин G", "Цефтриаксон", "Ванкомицин", "Ампициллин"])),
            ("Coagulase-negative staphylococci", json.dumps(["Ванкомицин", "Линезолид", "Рифампицин"])),
        ]
        cur.executemany("INSERT INTO pathogens (name, sensitive_drugs) VALUES (?,?)", pathogens_data)
    
    conn.commit()
    conn.close()

# ---------- ФУНКЦИИ ДЛЯ ПОЛУЧЕНИЯ ДАННЫХ ----------
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
    return None

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

@app.route('/api/pathogen_sensitivity', methods=['GET'])
def pathogen_sensitivity():
    name = request.args.get('name')
    sens = get_pathogen_sensitivity(name)
    return jsonify(sens if sens else [])

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
    dose_adj_line1 = get_dose_adjustment(protocol["line1"].split('+')[0].strip(), crcl) if protocol["line1"] else ""
    
    return jsonify({
        "stratification": risk_level,
        "crcl": round(crcl, 1),
        "sofa_text": sofa_text,
        "lab": lab,
        "line1": protocol["line1"], "line1_dose": protocol["line1_dose"], "line1_duration": protocol["line1_duration"],
        "line2": protocol["line2"], "line2_dose": protocol["line2_dose"], "line2_duration": protocol["line2_duration"],
        "line3": protocol["line3"], "line3_dose": protocol["line3_dose"], "line3_duration": protocol["line3_duration"],
        "allergy_alt": protocol["allergy_alt"], "allergy_alt_dose": protocol["allergy_alt_dose"], "allergy_alt_duration": protocol["allergy_alt_duration"],
        "renal_alt": protocol["renal_alt"], "renal_alt_dose": protocol["renal_alt_dose"], "renal_alt_duration": protocol["renal_alt_duration"],
        "note": protocol["note"],
        "dose_adjustment": dose_adj_line1,
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
    if not sensitive_all:
        return jsonify({"error": f"Нет данных о чувствительности для {pathogen}"}), 404
    
    recommended = [drug for drug in sensitive_all if drug not in resistance_drugs]
    if not recommended:
        return jsonify({"error": "Нет доступных препаратов с учётом резистентности. Требуется консультация микробиолога."}), 404
    
    result = []
    for drug in recommended[:4]:
        dose_adj = get_dose_adjustment(drug, crcl)
        result.append({
            "drug": drug,
            "dose_adjustment": dose_adj,
            "note": "Длительность зависит от локализации и клинического ответа (обычно 7-14 дней)"
        })
    
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
