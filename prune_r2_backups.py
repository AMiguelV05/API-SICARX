"""
Script independiente (no lo importa la app) que poda el bucket R2 de
respaldos cuando el uso total se acerca al limite del plan gratuito de
Cloudflare R2 (10 GB). El objetivo es NUNCA pagar por almacenamiento de
respaldos, no solo evitarlo la mayoria del tiempo - por eso esto corre en
cada ejecucion del workflow de respaldo en vez de confiar unicamente en las
reglas de expiracion por edad (7 dias `frequent/`, 35 dias `daily/`).

Prioriza borrar primero los objetos mas antiguos bajo `frequent/` (la capa de
corta duracion, disenada para ser barata de perder) antes de tocar `daily/`
(la capa de largo plazo). Si hace falta podar `daily/` para volver a estar
dentro del presupuesto, es una senal de que los supuestos de tamano de este
diseno estan mal - el script termina con un codigo de error distinto de cero
en ese caso para que la notificacion de falla de GitHub Actions avise.

Requiere que las reglas de "bucket lock" en R2 sean mas cortas que la
retencion normal (ver CLAUDE.md, seccion "Disaster recovery and backups") -
un lock igual a la retencion completa bloquearia este script tanto como a un
atacante con las credenciales filtradas.

Variables de entorno esperadas (las mismas que ya usa .github/workflows/backup.yml
para subir a R2): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME.
"""

import os
import sys

import boto3

BUDGET_BYTES = 9 * 1024**3  # 9 GiB - deja margen bajo el limite gratuito de 10 GB


def main() -> None:
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    bucket = os.environ["R2_BUCKET_NAME"]

    objects = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects.extend(page.get("Contents", []))

    total = sum(obj["Size"] for obj in objects)
    print(f"Uso actual de R2: {total / 1024**2:.1f} MB")

    if total <= BUDGET_BYTES:
        print("Dentro del presupuesto, nada que podar.")
        return

    print(f"Excede el presupuesto de {BUDGET_BYTES / 1024**3:.1f} GB - podando.")

    frequent = sorted(
        (o for o in objects if o["Key"].startswith("frequent/")),
        key=lambda o: o["LastModified"],
    )
    daily = sorted(
        (o for o in objects if o["Key"].startswith("daily/")),
        key=lambda o: o["LastModified"],
    )

    deleted_daily = False
    for obj in frequent + daily:
        if total <= BUDGET_BYTES:
            break
        is_daily = obj["Key"].startswith("daily/")
        try:
            client.delete_object(Bucket=bucket, Key=obj["Key"])
        except Exception as exc:  # noqa: BLE001 - probablemente un lock activo
            print(f"No se pudo borrar {obj['Key']}: {exc}")
            continue
        total -= obj["Size"]
        deleted_daily = deleted_daily or is_daily
        print(f"Borrado {obj['Key']} ({obj['Size'] / 1024**2:.1f} MB)")

    if total > BUDGET_BYTES:
        print(
            f"Sigue excediendo el presupuesto ({total / 1024**2:.1f} MB) despues de podar "
            "todo lo que se pudo borrar (probablemente por locks activos) - revisar manualmente."
        )
        sys.exit(1)

    if deleted_daily:
        print(
            "ADVERTENCIA: se tuvo que borrar respaldos 'daily' antes de su expiracion "
            "normal para mantenerse dentro del presupuesto - revisar la frecuencia/retencion."
        )


if __name__ == "__main__":
    main()
