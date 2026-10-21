from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='EmployeeSyncRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_employee_id', models.CharField(max_length=100, unique=True)),
                ('email', models.EmailField(blank=True, max_length=254, null=True)),
                ('first_name', models.CharField(blank=True, max_length=100, null=True)),
                ('last_name', models.CharField(blank=True, max_length=100, null=True)),
                ('department', models.CharField(blank=True, max_length=200, null=True)),
                ('designation', models.CharField(blank=True, max_length=200, null=True)),
                ('last_status', models.CharField(default='PENDING', max_length=30)),
                ('last_error_message', models.TextField(blank=True, default='')),
                ('last_error_code', models.CharField(blank=True, default='', max_length=50)),
                ('attempt_count', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'myapp_employee_sync_record',
            },
        ),
    ]
