# HHInventory

A basic Django project set up to run locally and deploy cheaply to AWS via
Elastic Beanstalk (single-instance mode).

## Stack

- Django 5.2 (LTS)
- gunicorn (app server)
- PostgreSQL via RDS in production, SQLite for local dev
- S3 (via django-storages) for static/media files in production
- Elastic Beanstalk, single-instance environment (no load balancer) — the
  cheapest standard AWS deployment path for a low-traffic site

## Local setup

```bash
python -m venv venv
source venv/bin/activate  # venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env      # edit DJANGO_SECRET_KEY at minimum
python manage.py migrate
python manage.py runserver
```

Visit `http://127.0.0.1:8000/health/` — should return `{"status": "ok"}`.

## Configuration

All settings are driven by environment variables (see
[.env.example](.env.example)), loaded from a local `.env` file in
development and from the Elastic Beanstalk environment in production. No
secrets are committed to the repo.

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django's `SECRET_KEY`. Required. |
| `DJANGO_DEBUG` | `True`/`False`. Always `False` in production. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts, e.g. your EB domain. |
| `DATABASE_URL` | `postgres://user:pass@host:5432/dbname`. Unset = SQLite. |
| `USE_S3` | `True` to serve static/media from S3. |
| `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_REGION_NAME` | S3 bucket for static/media. |

## Deploying to AWS (Elastic Beanstalk, single-instance)

This avoids the cost of a load balancer / NAT gateway, which is the main
line item for a low-traffic site.

### 1. Create supporting resources

- **S3 bucket** for static/media (e.g. `hhinventory-static`). Block public
  access as appropriate; static files can be served via the bucket's
  regional endpoint or fronted later with CloudFront.
- **RDS PostgreSQL**, free-tier eligible instance class
  (`db.t3.micro` / `db.t4g.micro`), in the same VPC/region you'll deploy to.
  Note the connection string for `DATABASE_URL`.

### 2. IAM

Attach a policy to the EB environment's EC2 instance profile granting it
`s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:DeleteObject` on the
static/media bucket only. The app uses this instance role automatically —
no AWS access keys are stored anywhere in the app or environment variables.

### 3. Initialize and create the environment

```bash
pip install awsebcli
eb init -p python-3.12 hhinventory
eb create hhinventory-env --single --instance-type t3.micro
```

`--single` creates a single-instance environment (no load balancer).

### 4. Set environment variables

```bash
eb setenv DJANGO_SECRET_KEY=... DJANGO_DEBUG=False \
  DJANGO_ALLOWED_HOSTS=your-env.elasticbeanstalk.com \
  DATABASE_URL=postgres://user:pass@your-rds-endpoint:5432/dbname \
  USE_S3=True AWS_STORAGE_BUCKET_NAME=hhinventory-static AWS_S3_REGION_NAME=us-east-1
```

### 5. Deploy

```bash
eb deploy
```

Migrations and `collectstatic` run automatically on deploy via
[.ebextensions/django.config](.ebextensions/django.config).

### 6. Create a Django superuser (one-time)

```bash
eb ssh
source /var/app/venv/*/bin/activate
cd /var/app/current
python manage.py createsuperuser
```

## Project layout

```
hhinventory/       Project settings, root urls, wsgi/asgi entrypoints
core/               Minimal app; health check endpoint at /health/
.ebextensions/      EB deploy hooks (migrate, collectstatic)
Procfile            Tells EB how to start gunicorn
requirements.txt    Python dependencies
.env.example        Documents required environment variables
```
