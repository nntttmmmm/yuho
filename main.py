import io
import json
import os
import re
import time
import zipfile
from datetime import date, datetime
from html.parser import HTMLParser
import requests
from google import genai

# 環境変数から取得
EDINET_API_KEY = os.environ.get("EDINET_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

PROCESSED_DOCS_FILE = "processed_docs.json"
BATCH_LIMIT = 10
SLEEP_SECONDS = 5


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.parts.append(text)

    def get_text(self):
        return "\n".join(self.parts)


def html_or_xml_to_text(raw_bytes):
    """バイト列（HTML / XML / XBRL / テキスト）から読みやすいテキストを抽出する"""
    for enc in ("utf-8", "cp932", "shift_jis", "latin-1"):
        try:
            decoded = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        decoded = raw_bytes.decode("utf-8", errors="ignore")

    parser = _TextExtractor()
    try:
        parser.feed(decoded)
        extracted = parser.get_text()
    except Exception:
        extracted = re.sub(r"<[^>]+>", " ", decoded)

    return re.sub(r"\n{3,}", "\n\n", extracted)


def load_processed_doc_ids(filepath=PROCESSED_DOCS_FILE):
    if not os.path.exists(filepath):
        return set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except Exception as e:
        print(f"処理済みリスト読み込みエラー: {e}")
        return set()


def save_processed_doc_ids(doc_ids, filepath=PROCESSED_DOCS_FILE):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(sorted(doc_ids), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"処理済みリスト保存エラー: {e}")


def format_submit_datetime(submit_dt_str):
    """EDINETの提出日時文字列を読みやすい形に整形する"""
    if not submit_dt_str:
        return "不明"
    s = str(submit_dt_str).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return s


def get_edinet_documents():
    """EDINET APIから本日の有価証券報告書（コード: 120）の一覧を取得する"""
    today = date.today().strftime("%Y-%m-%d")
    url = (
        f"https://api.edinet-fsa.go.jp/api/v2/documents.json"
        f"?date={today}&type=2&Subscription-Key={EDINET_API_KEY}"
    )

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])

        target_docs = []
        for doc in results:
            if doc.get("docTypeCode") == "120":
                target_docs.append(
                    {
                        "docID": doc.get("docID"),
                        "filerName": doc.get("filerName") or "提出者不明",
                        "submitDateTime": format_submit_datetime(doc.get("submitDateTime")),
                        "docDescription": doc.get("docDescription") or "",
                    }
                )
        return target_docs
    except Exception as e:
        print(f"EDINET API取得エラー: {e}")
        return []


def fetch_mda_order_backlog_excerpt(doc_id, max_chars=12000):
    """
    docID から書類を取得し、
    「第2 4 経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析」付近と
    「受注残高」の記載周辺テキストを抽出する。
    """
    content = None
    for doc_type in ("1", "2"):
        url = (
            f"https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}"
            f"?type={doc_type}&Subscription-Key={EDINET_API_KEY}"
        )
        try:
            res = requests.get(url, timeout=60)
            if res.status_code == 200 and res.content:
                content = res.content
                break
        except Exception as e:
            print(f"書類取得エラー ({doc_id}, type={doc_type}): {e}")

    if not content:
        return ""

    texts = []
    if zipfile.is_zipfile(io.BytesIO(content)):
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                lower = name.lower()
                if lower.endswith((".htm", ".html", ".xhtml", ".xbrl", ".xml", ".txt")):
                    try:
                        raw = zf.read(name)
                        t = html_or_xml_to_text(raw)
                        if t:
                            texts.append(t)
                    except Exception:
                        continue
    else:
        t = html_or_xml_to_text(content)
        if t:
            texts.append(t)

    full_text = "\n".join(texts)
    if not full_text or "受注残高" not in full_text:
        return ""

    mda_patterns = [
        r"経営者による財政状態、経営成績及びキャッシュ[・･]フローの状況の分析",
        r"第2\s*[【\[]?\s*4\s*[】\]]?\s*経営者による財政状態",
    ]
    start_pos = 0
    for pat in mda_patterns:
        m = re.search(pat, full_text)
        if m:
            start_pos = max(0, m.start() - 500)
            break

    mda_section = full_text[start_pos : start_pos + max_chars * 2]

    idx = mda_section.find("受注残高")
    if idx != -1:
        begin = max(0, idx - 3000)
        end = min(len(mda_section), idx + 9000)
        return mda_section[begin:end][:max_chars]

    idx_all = full_text.find("受注残高")
    if idx_all != -1:
        begin = max(0, idx_all - 3000)
        end = min(len(full_text), idx_all + 9000)
        return full_text[begin:end][:max_chars]

    return ""


def has_order_backlog_in_mda(filer_name, submit_dt, excerpt_text):
    """
    「第2 4 経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析」に
    「受注残高」の記載があるか判定する。
    """
    if not excerpt_text:
        return False

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
以下の有価証券報告書の抜粋を読み、
「第2 4 経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析」に
「受注残高」に関する記載が含まれているか判定してください。
記載がある場合は「あり」、ない場合は「なし」とだけ答えてください。

提出者: {filer_name}
提出日時: {submit_dt}

【抜粋テキスト】
{excerpt_text}
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
    )
    answer = (response.text or "").strip()
    return answer.startswith("あり")


def send_discord_notification(message):
    """Discordへ通知を送る"""
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL が未設定のためDiscord通知をスキップします")
        return False
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=20)
        return res.status_code < 400
    except Exception as e:
        print(f"Discord通知エラー: {e}")
        return False


def main():
    print("EDINETのチェックを開始します...")
    all_docs = get_edinet_documents()
    processed_ids = load_processed_doc_ids()

    unprocessed_docs = [
        doc for doc in all_docs if doc.get("docID") and doc["docID"] not in processed_ids
    ]
    target_docs = unprocessed_docs[:BATCH_LIMIT]

    print(
        f"本日の有報該当: {len(all_docs)}件 / 未処理: {len(unprocessed_docs)}件 / 今回処理: {len(target_docs)}件"
    )

    for doc in target_docs:
        doc_id = doc["docID"]
        filer_name = doc["filerName"]
        submit_dt = doc["submitDateTime"]

        try:
            excerpt = fetch_mda_order_backlog_excerpt(doc_id)
            found = has_order_backlog_in_mda(filer_name, submit_dt, excerpt)

            if found:
                notify_text = (
                    f"📌【有報・受注残高の記載あり】\n"
                    f"提出者: {filer_name}\n"
                    f"提出日時: {submit_dt}"
                )
                if send_discord_notification(notify_text):
                    print(f"Discord通知完了: {filer_name} ({submit_dt}) [{doc_id}]")
                else:
                    print(f"Discord通知失敗のため記録をスキップ: {filer_name} [{doc_id}]")
                    continue
            else:
                print(f"受注残高の記載なし: {filer_name} ({submit_dt}) [{doc_id}]")

            processed_ids.add(doc_id)
            save_processed_doc_ids(processed_ids)

        except Exception as e:
            print(f"処理エラー（次回再試行対象） {filer_name} [{doc_id}]: {e}")

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
