# PostgreSQL → S3 backup service

Автономный Docker-сервис резервных копий PostgreSQL. Не зависит от backend
или FastAPI. Версия разработки 0.1.0; удалённый репозиторий ещё не создан.
Использует штатные pg_dump/pg_restore и AWS boto3; наша обвязка — расписание,
догоняющий запуск, readback SHA-256, состояние и ротация по количеству.

## Настройки

| Переменная | Назначение |
| --- | --- |
| POSTGRES_HOST / PORT / DB / USER / PASSWORD | Исходная база, порт по умолчанию 5432 |
| BACKUP_S3_ENDPOINT / BUCKET / REGION | HTTPS endpoint, бакет, регион (по умолчанию ru-1) |
| BACKUP_S3_PREFIX | Префикс, по умолчанию postgres-backups/production; имя БД добавляется автоматически |
| AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY | Ключи S3 |
| BACKUP_KEEP | Число успешных копий каждой категории, по умолчанию 30 |
| BACKUP_TIME / BACKUP_TIMEZONE | HH:MM и IANA timezone, по умолчанию 17:00, Europe/Moscow |
| BACKUP_STATE_DIR | Постоянный каталог состояния, по умолчанию /state |

В `/state` монтируется постоянный том. UID/GID контейнера — 10001.
Для другого бакета/префикса используется отдельное состояние. Секреты передаются
через приватный env_file, не включаются в образ или Git.

## Команды

- `serve` — расписание и одна догоняющая копия после пропуска.
- `run --category daily` — дополнительная ежедневная копия сейчас.
- `run --category predeploy` — копия перед миграцией с отдельным лимитом.
- `status` — результат и метаданные без секретов.
- `download --manifest <S3-key.json> --output /state/restore.dump` — скачать
  с проверкой SHA-256; существующий файл не перезаписывается.

Весь новый dump читается обратно из S3: учитывать исходящий трафик тарифа.
Manifest публикуется после проверки архива и тоже читается обратно.
Неудачный бэкап не удаляет старые копии. Повтор после сбоя использует
сохранённый архив и тот же ключ. Ночь/выходные без включённого ПК — норма;
uptime-контроля нет. Ошибка очистки не отменяет успешный бэкап.

Начальный образ содержит PostgreSQL client 17; для другой версии сервера
выбирается совместимый POSTGRES_IMAGE и проверяется восстановление.
Дамп одной базы не включает роли и секреты всего кластера. Шифрование самих
файлов не реализовано, транспорт защищён HTTPS. GlitchTip будет подключён
отдельно; пока ошибки доступны в логах/status.

## Разработка

```sh
poetry install --no-root
poetry run python scripts/export_requirements.py
poetry run ruff check src scripts tests
poetry run pyright
docker build --target test -t postgres-s3-backup:test .
docker run --rm postgres-s3-backup:test
docker build --target runtime -t postgres-s3-backup:dev .
```

`tests/smoke.ps1` проверяет dump → S3-эмулятор → скачивание → реальное
восстановление в отдельную тестовую PostgreSQL. Реальные S3-ключи не нужны.
