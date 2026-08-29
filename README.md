# Best PDF Robot

Telegram-бот для четырёх операций:

- создание PDF из 1-15 фотографий без обрезки;
- сжатие PDF с окончанием имени `_compresed.pdf`;
- переименование PDF;
- разделение PDF на отдельные файлы или ZIP.

Пользовательские файлы обрабатываются во временном каталоге и удаляются сразу
после отправки результата. Пятнадцатиминутки этот бот не создаёт.

## Запуск

```bash
sudo apt-get install ghostscript
cp .env.example .env
nano .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```
