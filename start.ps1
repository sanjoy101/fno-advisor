Set-Location C:\laragon\www\fno-advisor
& .\.venv\Scripts\Activate.ps1
python -c "import sys; assert '.venv' in sys.executable, 'WRONG PYTHON'; print('venv ok:', sys.version.split()[0])"