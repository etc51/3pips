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
sudo docker compose up -d --build
```

## 4. Проверить

```bash
sudo docker compose ps
sudo docker compose logs -f paper
```

## 5. Остановить

```bash
cd /opt/3pips
sudo docker compose down
```

## 6. Перезапустить

```bash
cd /opt/3pips
sudo docker compose restart paper
```

## 7. Обновить

```bash
cd /opt/3pips
sudo docker compose down
git pull
sudo docker compose up -d --build
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
scp user@SERVER_IP:/opt/3pips/reports/archives/*.tar.gz .
```

## 11. Что запускается

```text
paper      - сам бот, supervisor и dashboard
archive    - ежедневная упаковка истории в reports/archives
```

Сейчас это paper-бот, не реальный live.
