# 3pips: установка paper-бота на Ubuntu

Эта инструкция для сервера друга на Ubuntu.

## 1. Установка на сервере

Выполнять от пользователя, у которого есть `sudo`.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
python3 --version

sudo mkdir -p /opt
cd /opt
sudo git clone https://github.com/etc51/3pips.git 3pips
cd /opt/3pips

bash scripts/install_ubuntu.sh
```

Если репозиторий уже склонирован:

```bash
cd /opt/3pips
sudo git pull
bash scripts/install_ubuntu.sh
```

Если `python3 --version` ниже `3.11`, сначала поставить Python `3.11+`, затем повторить установку.

T-Bank SDK ставится установщиком из package index Т-Банка:

```text
https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

## 2. Запуск, остановка, перезапуск

```bash
sudo systemctl start 3pips-paper
sudo systemctl stop 3pips-paper
sudo systemctl restart 3pips-paper
sudo systemctl status 3pips-paper --no-pager
```

Включить автозапуск после перезагрузки:

```bash
sudo systemctl enable 3pips-paper
```

Отключить автозапуск:

```bash
sudo systemctl disable 3pips-paper
```

## 3. Логи и проверка работы

Лог systemd:

```bash
journalctl -u 3pips-paper -f
```

Лог supervisor:

```bash
tail -f /opt/3pips/reports/runtime/v7_paper_supervisor_20260525.log
```

Ежедневный архив всей истории создается в:

```bash
/opt/3pips/reports/archives/
```

Отдельно каждый день собирается raw ZIP за прошедшую сессию. Если настроен SMTP, этот ZIP уходит на:

```text
etc00051@yandex.ru
```

Проверить архиватор:

```bash
systemctl list-timers 3pips-archive.timer --no-pager
sudo systemctl start 3pips-archive.service
ls -lh /opt/3pips/reports/archives/
```

Однократная настройка почтовой отправки:

```bash
sudo mkdir -p /opt/3pips/secrets
sudo nano /opt/3pips/secrets/archive_smtp_password.txt
sudo chmod 600 /opt/3pips/secrets/archive_smtp_password.txt
sudo chown 3pips:3pips /opt/3pips/secrets/archive_smtp_password.txt
sudo nano /etc/3pips/3pips.env
sudo systemctl restart 3pips-archive.timer
```

В `/etc/3pips/3pips.env` должны быть:

```text
ARCHIVE_EMAIL_ENABLED=1
ARCHIVE_EMAIL_TO=etc00051@yandex.ru
ARCHIVE_EMAIL_FROM=etc00051@yandex.ru
ARCHIVE_SMTP_HOST=smtp.yandex.ru
ARCHIVE_SMTP_PORT=465
ARCHIVE_SMTP_USER=etc00051@yandex.ru
ARCHIVE_SMTP_PASSWORD_FILE=/opt/3pips/secrets/archive_smtp_password.txt
ARCHIVE_SMTP_USE_SSL=1
ARCHIVE_SMTP_STARTTLS=0
ARCHIVE_DAILY_TIME=23:59
```

Проверить health всех контуров:

```bash
cd /opt/3pips
for f in reports/paper_runs/v7_live_20260525/*_health.json; do
  echo "---- $f"
  cat "$f"
done
```

Открытые paper-позиции:

```bash
cd /opt/3pips
for f in reports/paper_runs/v7_live_20260525/*_paper_open_positions.json; do
  echo "---- $f"
  cat "$f"
done
```

Сделки:

```bash
ls -lh /opt/3pips/reports/paper_runs/v7_live_20260525/*_multi_futures_paper_trades.csv
```

## 4. Как владельцу смотреть dashboard

Безопасный способ: SSH-туннель. На своем компьютере:

```bash
ssh -L 8768:127.0.0.1:8768 user@SERVER_IP
```

Потом открыть в браузере:

```text
http://127.0.0.1:8768/
```

`user@SERVER_IP` заменить на логин и IP сервера.

Так dashboard не надо открывать наружу в интернет.

## 5. Если хочется открыть dashboard наружу

Лучше не делать. Если все же нужно, открывать только на конкретный IP:

```bash
sudo ufw allow from YOUR_HOME_IP to any port 8768 proto tcp
```

Но текущий dashboard слушает `127.0.0.1`, поэтому стандартный вариант контроля - SSH-туннель.

## 6. Обновление бота

```bash
cd /opt/3pips
sudo systemctl stop 3pips-paper
sudo git pull
bash scripts/install_ubuntu.sh
sudo systemctl start 3pips-paper
```

Проверка после обновления:

```bash
sudo systemctl status 3pips-paper --no-pager
journalctl -u 3pips-paper -n 100 --no-pager
```

## 7. Что запускается

Сервис `3pips-paper` запускает Python-supervisor:

```bash
/opt/3pips/scripts/ubuntu_paper_supervisor.py
```

Supervisor держит живыми:

- `classic_core`
- `gl_watch`
- `neo`
- `tail_research`
- dashboard на порту `8768`

Если контур упал, health устарел или dashboard перестал отвечать, supervisor перезапускает нужный процесс.

## 8. Где лежат рабочие файлы

```text
/opt/3pips/reports/runtime/
/opt/3pips/reports/paper_runs/v7_live_20260525/
/opt/3pips/reports/archives/
```

Главные файлы:

```text
*_health.json
*_paper_open_positions.json
*_multi_futures_paper_trades.csv
*_gpt_shadow_trades.csv
*_entry_audit.csv
*_live_orderbook_snapshots.csv
```

Забрать все архивы себе:

```bash
scp user@SERVER_IP:/opt/3pips/reports/archives/* .
```

Забрать всю рабочую историю без упаковки:

```bash
scp -r user@SERVER_IP:/opt/3pips/reports/paper_runs/v7_live_20260525 .
```

## 9. Важно перед реальным live

Этот сервис запускает paper-бота. Перед real live надо отдельно включать и проверять live executor.
Не менять paper на real без 15-минутного smoke-test с минимальным размером.
