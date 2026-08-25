# Best PDF Robot

Telegram-бот для трёх операций:

- создание PDF из 1-15 фотографий без обрезки;
- переименование PDF;
- разделение PDF на отдельные файлы или ZIP.

Пользовательские файлы обрабатываются во временном каталоге и удаляются сразу
после отправки результата. Пятнадцатиминутки этот бот не создаёт.

## Запуск

```bash
cp .env.example .env
nano .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```
