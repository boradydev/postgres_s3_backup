param([string]$Image = 'postgres-s3-backup:dev')
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$smokeId = 'pgs3-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$network = $smokeId
$database = "$smokeId-db"
$storage = "$smokeId-s3"
$volume = "$smokeId-state"
$download = "$smokeId-download"
$outputDirectory = Join-Path $PSScriptRoot '../.test-output'
New-Item -ItemType Directory -Force $outputDirectory | Out-Null
$archive = Join-Path (Resolve-Path $outputDirectory) "$smokeId.dump"
$postgres = 'postgres:17-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27'
$options = @('--rm', '--network', $network, '-v', "${volume}:/state",
    '-e', "POSTGRES_HOST=$database", '-e', 'POSTGRES_PORT=5432', '-e', 'POSTGRES_DB=backup_source',
    '-e', 'POSTGRES_USER=postgres', '-e', 'POSTGRES_PASSWORD=smoke-only',
    '-e', "BACKUP_S3_ENDPOINT=http://${storage}:5000", '-e', 'BACKUP_S3_BUCKET=smoke-backups',
    '-e', 'BACKUP_ALLOW_HTTP=1', '-e', 'BACKUP_S3_PREFIX=smoke',
    '-e', 'AWS_ACCESS_KEY_ID=smoke-only', '-e', 'AWS_SECRET_ACCESS_KEY=smoke-only')
try {
    docker network create $network | Out-Null
    docker volume create $volume | Out-Null
    docker run -d --name $database --network $network -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=backup_source $postgres | Out-Null
    docker run -d --name $storage --network $network motoserver/moto:5.1.16 | Out-Null
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $PSNativeCommandUseErrorActionPreference = $false
        $probe = docker exec $database pg_isready -U postgres -d backup_source 2>&1
        $probeExit = $LASTEXITCODE
        $PSNativeCommandUseErrorActionPreference = $true
        if ($probeExit -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Temporary PostgreSQL not ready' }
    $createBucket = @'
import time, boto3
from botocore.config import Config
from os import environ
s=boto3.client('s3',endpoint_url=environ['BACKUP_S3_ENDPOINT'],region_name='ru-1',config=Config(request_checksum_calculation='when_required'))
for attempt in range(40):
    try:
        s.create_bucket(Bucket='smoke-backups',CreateBucketConfiguration={'LocationConstraint':'ru-1'})
        break
    except Exception:
        if attempt == 39: raise
        time.sleep(1)
'@
    docker run @options --entrypoint python $Image -c $createBucket
    docker exec $database psql -U postgres -d backup_source -v ON_ERROR_STOP=1 -c "CREATE TABLE backup_probe(value text); INSERT INTO backup_probe VALUES ('verified-restore');" | Out-Null
    docker run @options $Image run
    $status = docker run @options $Image status | ConvertFrom-Json
    $manifest = $status.lastSuccess.manifestKey
    if (-not $manifest) { throw 'Missing success marker' }
    docker run @options $Image download --manifest $manifest --output /state/restore.dump
    docker create --name $download -v "${volume}:/state:ro" --entrypoint cat $Image /state/restore.dump | Out-Null
    docker cp "${download}:/state/restore.dump" $archive
    docker cp $archive "${database}:/tmp/restore.dump"
    docker exec $database createdb -U postgres restore_check
    docker exec $database pg_restore --exit-on-error --no-owner --no-acl -U postgres -d restore_check /tmp/restore.dump
    $value = docker exec $database psql -U postgres -d restore_check -Atc 'SELECT value FROM backup_probe'
    if ($value.Trim() -ne 'verified-restore') { throw 'Restored data mismatch' }
    Write-Output 'PASS: pg_dump -> S3 -> SHA-256 download -> pg_restore -> actual data read'
} finally {
    $PSNativeCommandUseErrorActionPreference = $false
    docker rm -f -v $download $database $storage 2>$null | Out-Null
    docker volume rm $volume 2>$null | Out-Null
    docker network rm $network 2>$null | Out-Null
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive }
}

