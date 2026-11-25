@echo off
cd /d %~dp0\..
python run_all.py --glf "C:\path\to\log_2025-06-19-203913.glf" --profile config\tanana_203913_profile.json --with-association
