import json
import os
import time
import requests
from google import genai

# 環境変数からAPIキーを取得
EDINET_API_KEY = os.environ.get("EDINET_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# 処理済みdocIDを保存するファイル名
PROCESSED_DOCS_FILE = "processed_docs.json"
# 1回の実行で処理する最大件数
BATCH_LIMIT = 10
# 1件ごとの待機秒数（429レート制限対策）
SLEEP_SECONDS = 5


def load_processed_doc_ids(filepath=PROCESSED_DOCS_FILE):
    """処理済みのdocID一覧を読み込む"""
    if not os.path.exists(filepath):
        return set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
            return set()
    except Exception as e:
        print(f"処理済みリスト読み込みエラー: {e}")
        return set()


def save_processed_doc_ids(doc_ids, filepath=PROCESSED_DOCS_FILE):
    """処理済みのdocID一覧を保存する"""
    try:
        sorted_ids = sorted(doc_ids)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(sorted_ids, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"処理済みリスト保存エラー: {e}")


def get_edinet_documents():
    """EDINET APIから本日の有価証券報告書（コード: 120）の一覧を取得する"""
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    url = f"https://api.edinet-fsa.go.jp/api/v2/documents.json?date={today}&type=2&Subscription-Key={EDINET_API_KEY}"

    try:
        response = requests.get(url)
        data = response.json()
        results = data.get("results", [])

        target_docs = []
        for doc in results:
            # 書類種別コード '120' が有価証券報告書
            if doc.get("docTypeCode") == "120":
                target_docs.append(
                    {
                        "docID": doc.get("docID"),
                        "filerName": doc.get("filerName"),
                        "docDescription": doc.get("docDescription"),
                    }
                )
        return target_docs
    except Exception as e:
        print(f"EDINET API取得エラー: {e}")
        return []


def analyze_with_gemini(company_name, text_snippet):
    """Gemini AIを使って受注実績の数値があるか判定・要約する"""
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
以下の企業（{company_name}）の有価証券報告書のテキストを読み、
「第2 経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析」の「受注実績」に関する記述が含まれているか確認してください。

【条件】
- 単なる「受注環境が厳しかった」などの文字の言及ではなく、具体的な「受注高」「受注残高」などの**数値（金額や前年比など）**が掲載されているか？
- 掲載されている場合、その数値の要点と前年比などを簡潔にまとめてください。
- 数値が載っていない場合は「数値なし」とだけ答えてください。

【テキスト】
{text_snippet}
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt,
    )
    return response.text


def send_discord_notification(message):
    """Discordへ通知を送る"""
    if not DISCORD_WEBHOOK_URL:
        print("Discord Webhook URLが設定されていません")
        return False
    payload = {"content": message}
    res = requests.post(DISCORD_WEBHOOK_URL, json=payload)
    return res.status_code < 400


def main():
    print("EDINETのチェックを開始します...")
    all_docs = get_edinet_documents()
    processed_ids = load_processed_doc_ids()

    # 未処理の書類だけを抽出
    unprocessed_docs = [
        doc for doc in all_docs if doc.get("docID") and doc["docID"] not in processed_ids
    ]

    # 1回の実行で最大BATCH_LIMIT件までに制限
    target_docs = unprocessed_docs[:BATCH_LIMIT]

    print(
        f"本日の有報該当: {len(all_docs)}件 / 未処理: {len(unprocessed_docs)}件 / 今回処理: {len(target_docs)}件"
    )

    for doc in target_docs:
        doc_id = doc["docID"]
        filer_name = doc["filerName"]

        try:
            # 本来はここでXBRLやテキストデータを取得しますが、
            # サンプルとして簡易的にAPIから取れる書類概要やテキストを取得する処理を想定
            # （※実際の運用では書類のテキストをここに入れ込みます）
            sample_text = f"会社名: {filer_name} の有価証券報告書が提出されました。"

            # AIに判定させる
            result = analyze_with_gemini(filer_name, sample_text)

            if "数値なし" not in result:
                msg = f"🎯【有報・受注実績の数値検知】\n企業名: {filer_name}\n\n{result}"
                sent = send_discord_notification(msg)
                if sent:
                    print(f"通知送信: {filer_name} ({doc_id})")
                else:
                    print(f"通知送信に失敗したため記録をスキップ: {filer_name} ({doc_id})")
                    continue
            else:
                print(f"数値なし: {filer_name} ({doc_id})")

            # Gemini判定・通知まで完了したものだけを処理済みとして記録（取りこぼし防止）
            processed_ids.add(doc_id)
            save_processed_doc_ids(processed_ids)

        except Exception as e:
            print(f"処理エラー（次回再試行対象） {filer_name} ({doc_id}): {e}")

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
