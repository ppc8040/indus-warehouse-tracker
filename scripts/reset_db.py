import os
import psycopg2

uri = os.environ.get('DATABASE_URL')
if uri.startswith('postgres://'):
    uri = uri.replace('postgres://', 'postgresql://', 1)

conn = psycopg2.connect(uri)
conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
cur = conn.cursor()

cur.execute('TRUNCATE TABLE movements RESTART IDENTITY CASCADE;')
cur.execute('TRUNCATE TABLE pallet_status RESTART IDENTITY CASCADE;')
cur.execute('TRUNCATE TABLE ivt_scans RESTART IDENTITY CASCADE;')
cur.execute('TRUNCATE TABLE error_log RESTART IDENTITY CASCADE;')

cur.close()
conn.close()
print('✓ All data tables truncated. Schema preserved.')
