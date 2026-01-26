   sudo apt update
  sudo apt install python3 python3-venv python3-pip
   python3 -m venv .venv



source .venv/bin/activate
   pip install pyinstaller pdfplumber pandas
pyinstaller --onefile --name rbc_parser extract_rbc_activity.py
chmod +x rbc_parser