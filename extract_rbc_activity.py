
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from configparser import ConfigParser

import pdfplumber
import pandas as pd

# ====== CONFIG ======
def load_config():
    if getattr(sys, "frozen", False):
        # Running as PyInstaller onefile binary
        base_dir = Path(sys.executable).resolve().parent
    else:
        # Running as plain .py script
        base_dir = Path(__file__).resolve().parent

    config_path = base_dir / "config.ini"

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    config =  ConfigParser()
    config.optionxform = str
    config.read(config_path)
    return config


# Load configuration
config = load_config()

# Extract configuration values
INPUT_DIR = config.get('paths', 'input_dir')
OUTPUT_CSV = config.get('paths', 'output_csv')

# Load action mappings
ACTION_MAP = dict(config.items('actions'))

# Load account mappings
ACCOUNT_MAP = dict(config.items('accounts'))

# Load transfer account settings
REINVEST_TRANSFER_ACCOUNT = config.get('transfer_accounts', 'reinvest')
CASH_SUFFIX =  config.get('transfer_accounts', 'cash_suffix')



RESP_GRANT = config.get('governement_grants', 'RESP')

# Example combined date tokens in some PDFs: Jan102025, Jun302025
DATE_TOKEN_RE = re.compile(r"^[A-Za-z]{3}\d{1,2}\d{4}$")
NUM_RE = re.compile(r"^[\d,]+(\.\d+)?$")


def to_number(s: str):
    """
    Convert strings like:
      "10" → 10.0
      "\"10\"" → 10.0
      "2,345.15" → 2345.15
      "\"2,345.15\"" → 2345.15
      "-1,234" → -1234.0

    Returns float OR original string if not numeric.
    """
    if not s:
        return ""

    # Strip surrounding quotes: "123.45" or '123.45'
    s = s.strip().strip('"').strip("'")

    # Remove thousand separators
    s_clean = s.replace(",", "")

    # Convert to float
    try:
        return float(s_clean)
    except ValueError:
        return s


def extract_date_and_rest(line):
    tokens = line.split()
    if tokens and DATE_TOKEN_RE.match(tokens[0]):
        date_disp = format_date_token(tokens[0])
        rest = " ".join(tokens[1:])  # everything after date token
    else:
        date_disp, rest = parse_line_with_three_part_date(line)
    return date_disp, rest


def format_date_token(tok: str) -> str:
    """
    Convert 'Jan102025' -> '2025-01-10'
    """
    month = tok[:3]
    day = tok[3:-4]
    year = tok[-4:]
    try:
        # Parse the date components and create a datetime object
        date_str = f"{month} {day} {year}"
        parsed_date = datetime.strptime(date_str, "%b %d %Y")
        # Format as ISO date
        return parsed_date.strftime("%Y-%m-%d")
    except ValueError:
        return tok


def parse_line_with_three_part_date(line: str):
    """
    Parse date when the line starts like: 'Dec 23 2024 ...'
    Returns (date_str, rest_of_line) or (None, None) if not matched.
    """
    m = re.match(
        r"^(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})\s+(?P<rest>.+)$",
        line,
    )
    if not m:
        return None, None

    month = m.group("month")
    day = m.group("day")
    year = m.group("year")
    rest = m.group("rest").strip()
    
    try:
        # Parse the date and format as ISO date
        date_str = f"{month} {day} {year}"
        parsed_date = datetime.strptime(date_str, "%b %d %Y")
        date_disp = parsed_date.strftime("%Y-%m-%d")
        return date_disp, rest
    except ValueError:
        return None, None


def split_description_and_numbers(s: str):
    """
    Returns (description_string, list_of_numbers).
    Description = everything before the first numeric token.
    """
    parts = s.split()
    desc_tokens = []
    num_tokens = []

    found_number = False
    for p in parts:
        if not found_number and (
            NUM_RE.match(p) or (p.startswith("-") and NUM_RE.match(p[1:]))
        ):
            found_number = True
            num_tokens.append(p)
        elif found_number:
            # all tokens after first number are also numbers
            num_tokens.append(p)
        else:
            desc_tokens.append(p)

    description = " ".join(desc_tokens)
    return description, num_tokens


def match_action(kind: str) -> str:
    k_norm = kind.replace(" ", "").replace("-", "").lower()
    for key, action in ACTION_MAP.items():
        key_norm = key.replace(" ", "").replace("-", "").lower()
        if key_norm in k_norm:
            return action
    return ""


def parse_activity_line(
    line: str,
    fund_code: str,
    fund_name: str,
    source_file: str,
    account_number: str | None,
):
    """
    Parse a single activity line (Contribution / ClosingBalance / ReturnofCapital / IncomeReinvested).

    Returns:
      (record_or_None, pending_return_state)
    """
    date_disp, rest = extract_date_and_rest(line)

    if date_disp and rest and any(key in rest for key in ACTION_MAP.keys()):

        action = match_action(rest.split()[0])
        description, nums = split_description_and_numbers(rest)
        if nums:
            # safe: take last 5 numeric values
            if len(nums) >= 5:
                amount, unit_price, units_txn, units_post, total_val = nums[-5:]
            else:
                return None

            transfer_account = REINVEST_TRANSFER_ACCOUNT if action == "Reinvest" else ACCOUNT_MAP[account_number] + ":" + CASH_SUFFIX

            record = {
                "Date": date_disp,
                "Description": description + " src = " + source_file,
                "Action": action,
                "Value": to_number(amount),
                "Price": to_number(unit_price),
                "Amount": to_number(units_txn),
                "Account": ACCOUNT_MAP[account_number] + ":" + fund_code,
                "Trancfer Account": transfer_account,
                "Units you own (post)": units_post,
                "Total value ($)": total_val,
                "Fund code": fund_code,
                "Fund name": fund_name,
            }

            return record
    return None


def parse_savings_line(
    line: str,
    fund_name: str,
    source_file: str,
    account_number: str | None,
):
    """
    Parse lines from the 'Your savings deposit activity' table.

    Format examples from TFSA PDF:
      'Apr122022 Contribution 200.00 3,301.22'
      'May272022 Withdrawal -2,000.00 1,901.22'
      'Jun302022 InterestReinvested 1.45 1,302.67'
      'Jun302022 ClosingBalance 1,302.67'

    We only have: Date, Transaction, Amount, Total value.
    Unit price / units are left blank.
    """
    date_disp, rest = extract_date_and_rest(line)

    if date_disp and rest and any(key in rest for key in ACTION_MAP.keys()):
        action = match_action(rest.split()[0])
        description, nums = split_description_and_numbers(rest)

        if nums:
            # safe: take last 5 numeric values
            if len(nums) >= 2:
                amount, total_val = nums[-2:]
            else:
                return None

            transfer_account = REINVEST_TRANSFER_ACCOUNT if action == "Reinvest" else ACCOUNT_MAP[account_number] + ":" + CASH_SUFFIX
            record = {
                "Date": date_disp,
                "Description": description + " src = " + source_file,
                "Action": action,
                "Value": to_number(amount),
                "Price": "",
                "Amount": "",
                "Account": ACCOUNT_MAP[account_number] + ":SavingsDeposit",
                "Trancfer Account": transfer_account,
                "Units you own (post)": "",
                "Total value ($)": to_number(total_val),
                "Fund code": "",
                "Fund name": fund_name,
            }
            return record

    return None


def extract_account_number(lines):
    for i, line in enumerate(lines):
        if "Your account number" in line:
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                # extract the first consecutive digits in the next line
                m = re.search(r"\b(\d{6,12})\b", next_line)
                if m:
                    return m.group(1)
    return None


def extract_from_pdf_text(path: Path) -> list[dict]:
    """
    Parse the PDF by scanning raw text lines around:
      'Your investment activity with Royal Mutual Funds Inc.'
    and extracting activity lines per fund.
    """
    records: list[dict] = []

    with pdfplumber.open(path) as pdf:
        account_number = None
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = text.splitlines()

            if page.page_number == 1:
                account_number = extract_account_number(lines)

            if account_number is None:
                raise ValueError(f"Account number not found in PDF: {path.name}")

            in_section = False
            section_type = None  # 'mutual' or 'savings'
            current_fund_code = ""
            current_fund_name = ""
            pending_return: dict | None = None

            for raw in lines:
                line = raw.strip()

                # Enter section
                if not in_section:
                    # Mutual funds activity section
                    if "Your investment activity with Royal Mutual Funds Inc." in line:
                        in_section = True
                        section_type = "mutual"
                        continue

                    # Savings deposit activity section
                    if "Your savings deposit activity" in line:
                        in_section = True
                        section_type = "savings"
                        # We know this whole table is for RBC Savings Deposit
                        current_fund_name = "RBC Savings Deposit"
                        current_fund_code = ""
                        continue

                    continue

                # Heuristic: end section at page footer
                if line.startswith("Page") and "of" in line:
                    in_section = False
                    current_fund_code = ""
                    section_type = None
                    current_fund_name = ""
                    pending_return = None
                    continue

                # Detect fund header lines, e.g.:
                # 'RBC Select Balanced Portfolio - Sr. A (RBF460)'
                m_fund = re.match(r"(.+?)\s*\((RBF\d{3})\)(?:\s*\(continued\))?$", line)
                if m_fund:
                    current_fund_name = m_fund.group(1).strip()
                    current_fund_code = m_fund.group(2).strip()
                    continue

                # Detect RBC Savings Deposit fund header
                if "Savings Deposit" in line and "RBC" in line:
                    current_fund_name = "RBC Savings Deposit"
                    current_fund_code = ""
                    continue

                # If we have a pending Return of Capital, next line might be '(0.0187000)' etc.
                m_price = re.match(r"^\(([\d,]+\.\d+)\)$", line)
                if m_price and pending_return is not None:
                    pending_return["Unit price ($)"] = m_price.group(1)
                    records.append(pending_return)
                    pending_return = None
                    continue

                # Parse activity rows
                if section_type == "mutual":
                    rec= parse_activity_line(
                        line=line,
                        fund_code=current_fund_code,
                        fund_name=current_fund_name,
                        source_file=path.name,
                        account_number=account_number,
                    )
                    if rec is not None:
                        records.append(rec)
                        if rec["Description"].startswith(("Grant", "PGQC")) :
                            rec_income = {
                                "Date": rec["Date"],
                                "Description": rec["Description"],
                                "Action": rec["Action"],
                                "Value": rec["Value"],
                                "Price": "",
                                "Amount": "",
                                "Account": rec["Account"].replace(rec["Fund code"], CASH_SUFFIX),
                                "Trancfer Account": RESP_GRANT,
                                "Units you own (post)":"",
                                "Total value ($)": "",
                                "Fund code": "",
                                "Fund name": "",
                                }
                            records.append(rec_income)

                elif section_type == "savings":
                    rec = parse_savings_line(
                        line=line,
                        fund_name=current_fund_name or "RBC Savings Deposit",
                        source_file=path.name,
                        account_number=account_number,
                    )
                    if rec is not None:
                        records.append(rec)

    return records


def main():
    all_records: list[dict] = []

    input_dir = Path(INPUT_DIR)
    if not input_dir.exists():
        print(f"Input folder not found: {input_dir.resolve()}")
        return

    for fname in os.listdir(input_dir):
        if not fname.lower().endswith(".pdf"):
            continue

        pdf_path = input_dir / fname
        print(f"Processing: {pdf_path}")
        try:
            recs = extract_from_pdf_text(pdf_path)
            all_records.extend(recs)
        except Exception as e:
            print(f"Error processing {pdf_path}: {e}")

    if not all_records:
        print("No records found. Check folder path or PDF layout.")
        return

    df = pd.DataFrame(all_records)

    # Ensure column order is exactly as requested
    columns = [
        "Date",
        "Description",
        "Action",
        "Value",
        "Price",
        "Amount",
        "Account",
        "Trancfer Account",
        "Units you own (post)",
        "Total value ($)",
        "Fund code",
        "Fund name",
    ]
    df = df[columns]

    df.to_csv(OUTPUT_CSV, index=False)
    print(f"Done. Saved {len(df)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
