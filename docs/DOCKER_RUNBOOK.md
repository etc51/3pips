# 3pips: запуск через Docker на Ubuntu

## 1. Установить Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME})
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl start docker
sudo docker run hello-world
```

## 2. Скачать проект

```bash
sudo mkdir -p /opt/3pips
sudo chown -R $USER:$USER /opt/3pips
git clone https://github.com/etc51/3pips.git /opt/3pips
cd /opt/3pips
```

Если проект уже скачан:

```bash
cd /opt/3pips
git pull
```

## 3. Запустить

```bash
cd /opt/3pips
sudo docker compose --env-file /etc/3pips/3pips.env up -d --build
```

Если нужен ежедневный raw ZIP на почту, заранее положить SMTP-пароль:

```bash
cd /opt/3pips
mkdir -p secrets
nano secrets/archive_smtp_password.txt
chmod 600 secrets/archive_smtp_password.txt
```

И включить в окружении:

```bash
sudo nano /etc/3pips/3pips.env
```

Минимально:

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

## 4. Проверить

```bash
sudo docker compose ps
sudo docker compose logs -f paper
```

## 5. Остановить

```bash
cd /opt/3pips
sudo docker compose --env-file /etc/3pips/3pips.env down
```

## 6. Перезапустить

```bash
cd /opt/3pips
sudo docker compose --env-file /etc/3pips/3pips.env restart paper
```

## 7. Обновить

```bash
cd /opt/3pips
sudo docker compose --env-file /etc/3pips/3pips.env down
git pull
sudo docker compose --env-file /etc/3pips/3pips.env up -d --build
```

## 7.1 Включить автообновление

Автообновление подтягивает только то, что уже запушено в GitHub.
По умолчанию проверка идет каждые `10` минут и обновление пропускается, если есть открытые позиции.

```bash
cd /opt/3pips
chmod +x scripts/docker_autoupdate.sh
sudo mkdir -p /etc/3pips
sudo cp deploy/3pips-docker-autoupdate.service /etc/systemd/system/
sudo cp deploy/3pips-docker-autoupdate.timer /etc/systemd/system/
sudo cp deploy/3pips.env.example /etc/3pips/3pips.env
sudo systemctl daemon-reload
sudo systemctl enable 3pips-docker-autoupdate.timer
sudo systemctl start 3pips-docker-autoupdate.timer
```

Проверить таймер:

```bash
systemctl list-timers 3pips-docker-autoupdate.timer --no-pager
```

Запустить проверку вручную:

```bash
sudo systemctl start 3pips-docker-autoupdate.service
```

Посмотреть лог автообновления:

```bash
tail -f /opt/3pips/reports/runtime/docker_autoupdate.log
cat /opt/3pips/reports/runtime/docker_autoupdate_state.json
```

## 8. Dashboard

С сервера dashboard доступен на:

```text
http://127.0.0.1:8768/
```

С другого компьютера открыть через SSH-туннель:

```bash
ssh -L 8768:127.0.0.1:8768 user@SERVER_IP
```

Потом в браузере:

```text
http://127.0.0.1:8768/
```

## 9. Где будут логи и история

Все важное сохраняется на сервере вне контейнера:

```text
/opt/3pips/reports/runtime/
/opt/3pips/reports/paper_runs/
/opt/3pips/reports/archives/
```

Контейнер можно пересоздавать, история останется в этих папках.

## 10. Забрать архивы

```bash
scp user@SERVER_IP:/opt/3pips/reports/archives/* .
```

## 11. Что запускается

```text
paper      - сам бот, supervisor и dashboard
archive    - ежедневная упаковка истории в reports/archives и raw ZIP на почту
```

Сейчас это paper-бот, не реальный live.
