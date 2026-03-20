# coding: UTF-8
import asyncio
import websockets
import json
import uuid
import os
import re
import urllib.request
import urllib.parse
import urllib.error
import winsound
import google.generativeai as genai

# Gemini API の設定
# 環境変数などから取得するか、直接指定してください
API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyAtzAYqiMZONY_jfJ839vrYkcRt24J4PP4")
genai.configure(api_key=API_KEY)

# ラジオの全体的な人格と会話履歴を保持するリスト
# role: "user" は入力、role: "model" はGeminiの返答
chat_history = [
    {
        "role": "user", 
        "parts": ["あなたは深夜のインターネットラジオのパーソナリティです。落ち着いた、少しアンニュイで親しみやすいトーンでリスナーに語りかけます。流れてきたMisskeyのタイムラインの「今の雰囲気トレンド」や「要約」をもとに、深夜ラジオらしい、ゆったりとしたトークを展開してください。時にはリスナーの感情に寄り添ったり、クスッと笑えるようなツッコミを入れたりするのが得意です。話は長くなりすぎないように、簡潔にまとめてください。"]
    },
    {
        "role": "model", 
        "parts": ["こんばんは。こんな夜更けに、一緒に起きていてくれてありがとう。今日もタイムライン、いろんな言葉が行き交っているみたいですね……それじゃあ、少しだけ、みんなの呟きを覗いてみましょうか。"]
    }
]

async def fetch_notes(queue: asyncio.Queue):
    """Misskeyのグローバルタイムラインからノートを取得し、キューに入れるタスク"""
    instance_url = "misskey.io"
    uri = f"wss://{instance_url}/streaming"

    try:
        async with websockets.connect(uri) as websocket:
            channel_id = str(uuid.uuid4())
            connect_data = {
                "type": "connect",
                "body": {
                    "channel": "globalTimeline",
                    "id": channel_id
                }
            }
            
            await websocket.send(json.dumps(connect_data))
            print(f"Connected to {instance_url} Global Timeline...")

            while True:
                response = await websocket.recv()
                data = json.loads(response)

                if data.get("body", {}).get("id") == channel_id:
                    message = data["body"]
                    if message["type"] == "note":
                        note = message["body"]
                        user = note["user"]["name"] or note["user"]["username"]
                        text = note.get("text")
                        
                        if text:
                            # 取得したノートをキューに追加 (この瞬間も裏で動き続けます)
                            await queue.put({"user": user, "text": text})
    except Exception as e:
        print(f"WebSocket Error: {e}")

def synthesize_audio(text: str, speaker: int = 8) -> bytes:
    """VOICEVOXでテキストを音声合成し、WAVデータを返す（ブロッキング関数）"""
    base_url = "http://127.0.0.1:50021"
    
    try:
        query_payload = urllib.parse.urlencode({'text': text, 'speaker': speaker})
        req_query = urllib.request.Request(f"{base_url}/audio_query?{query_payload}", method="POST")
        with urllib.request.urlopen(req_query) as res:
            query_json = res.read()
            
            req_synth = urllib.request.Request(
                f"{base_url}/synthesis?speaker={speaker}", 
                data=query_json, 
                headers={'Content-Type': 'application/json'},
                method="POST"
            )
            with urllib.request.urlopen(req_synth) as res_synth:
                return res_synth.read()
                
    except urllib.error.URLError as e:
        print(f"\n[エラー] VOICEVOXとの通信に失敗しました。VOICEVOXが起動しているか確認してください。\n詳細: {e}")
        return b""

def play_audio(wav_data: bytes, fallback_wait: float = 5.0):
    """WAVデータを再生する（ブロッキング関数）"""
    if wav_data:
        winsound.PlaySound(wav_data, winsound.SND_MEMORY)
    else:
        import time
        time.sleep(fallback_wait)

async def radio_personality(queue: asyncio.Queue, audio_queue: asyncio.Queue):
    """キューから溜まったノートを取り出して、LLMにラジオ台本を作らせるタスク"""
    # Geminiモデルの準備 (gemini-1.5-flash または gemini-2.0-flash など)
    model = genai.GenerativeModel("gemini-3.1-flash-lite-preview") 
    
    print("ラジオパーソナリティが準備完了しました。ノートが溜まるのを待っています...")
    
    # 現在のトークテーマを保持
    current_theme = "最近の日常の些細な出来事"
    
    while True:
        # まず最低1件のノートが来るまで待機
        first_note = await queue.get()
        notes_chunk = [first_note]
        
        # 読み上げ中などに溜まっていた他のノートも、キューからすべて取り出す
        while not queue.empty():
            notes_chunk.append(queue.get_nowait())
            
        print(f"\n--- {len(notes_chunk)}件のノートを取得。構成作家が話題を整理中... ---")
        
        # ノートの結合
        notes_text = "\n".join([f"[{n['user']}]: {n['text'][:100]}" for n in notes_chunk])
        
        try:
            # ステップ1: 構成作家として、現在のテーマに関連するノートを抽出、またはテーマ変更を判断
            writer_prompt = f"""
あなたはラジオの構成作家です。現在の番組のトークテーマは「{current_theme}」です。
以下のMisskeyのタイムラインのノートから、このテーマに関連する、または話を広げられそうな面白いノートをいくつか（0〜3個）抽出してください。
もし現在のテーマに関連するノートが全くない場合、または同じ話題が長すぎて飽きが来ていると判断した場合は、タイムラインの中から次に面白そうな新しい話題を1つ決定し、テーマを変更してください。

【タイムライン】
{notes_text}

出力は以下のJSON形式のみで行ってください（Markdownの```json などの装飾は付けず、純粋なJSONテキストのみ出力してください）。
{{
    "next_theme": "継続するテーマまたは新しいテーマ",
    "is_theme_changed": true/false (テーマを変更したかどうか),
    "picked_notes": [
        "抽出したノートのテキスト(ユーザー名含む) 1",
        "抽出したノートのテキスト(ユーザー名含む) 2"
    ],
    "reason": "なぜそのテーマにしたか、なぜそのノートを選んだかの簡単な理由"
}}
"""
            
            response_writer = await asyncio.to_thread(model.generate_content, writer_prompt)
            writer_result = response_writer.text
            
            # JSON パース (安全のためMarkdownのコードブロック記法などを取り除く)
            clean_json = re.sub(r'```json\n|```', '', writer_result).strip()
            
            try:
                parsed_data = json.loads(clean_json)
                current_theme = parsed_data.get("next_theme", current_theme)
                is_changed = parsed_data.get("is_theme_changed", False)
                picked_notes = parsed_data.get("picked_notes", [])
                reason = parsed_data.get("reason", "")
                
                print(f"\n(構成作家の判断: 現在のテーマ「{current_theme}」 / 話題変更: {is_changed})")
                print(f"(ピックアップされたノート: {len(picked_notes)}件)")
            except Exception as e:
                print(f"\nJSON Parse Error: {e}\nRaw Output: {writer_result}")
                print("話題の抽出に失敗したため、少し待機してやり直します。")
                await asyncio.sleep(5)
                continue
            
            # ステップ2: 抽出された情報をもとに、ラジオ台本を生成させる
            picked_text = "\n".join([f"- {n}" for n in picked_notes]) if picked_notes else "（今回紹介するノートはありませんでした）"
            theme_intro = f"構成作家からの指示: 今回から話題を「{current_theme}」に変えてください。" if is_changed else f"構成作家からの指示: 引き続き話題は「{current_theme}」です。"
            
            prompt = f"""{theme_intro}
以下のノートがピックアップされました。
{picked_text}

構成作家のメモ: {reason}

これらを紹介しつつ（ノートがない場合はテーマについて自由にフリートークしてください）、深夜ラジオのパーソナリティとしてトークを展開してください。長くなりすぎないように、自然な相槌や感想を交えてリスナーに語りかけてください。前回までの会話の流れも引き継いで自然に繋げてください。"""
            
            # 履歴を利用して会話を生成
            global chat_history
            messages = chat_history + [{"role": "user", "parts": [prompt]}]
            
            # LLM API呼び出し (非同期処理として実行)
            response = await asyncio.to_thread(
                model.generate_content,
                messages
            )
            
            script = response.text
            print("\n================ ラジオ台本 ================")
            print(script)
            print("============================================\n")
            
            # 履歴の更新
            chat_history.append({"role": "user", "parts": [prompt]})
            chat_history.append({"role": "model", "parts": [script]})
            
            # 履歴が長くなりすぎてコンテキスト長を圧迫しないように、古いものを削除
            # 先頭の2つ（システムプロンプトの役割）は残す
            if len(chat_history) > 10:
                chat_history = chat_history[:2] + chat_history[-8:]
            
            print("\n(音声合成中... 裏で準備しています)")
            # 音声合成を実行してWAVデータを受け取る
            wav_data = await asyncio.to_thread(synthesize_audio, script, 8)
            
            print("(音声を再生キューに追加します。再生が終わるまで待機・ノート収集を続行します)")
            # 再生キューがいっぱい(maxsize=1)の場合は、今の再生が終わるまでここでブロックして待ちます
            await audio_queue.put((script, wav_data))
            
        except Exception as e:
            print(f"LLM Error: {e}")
            await asyncio.sleep(5) # エラー時は少し待機して再試行

async def audio_worker(audio_queue: asyncio.Queue):
    """生成された音声を順次再生するタスク"""
    while True:
        script, wav_data = await audio_queue.get()
        print("\n=== 🔊 音声を再生中 🔊 ===")
        # 再生中はブロッキングされますが、その間も他のタスク(WebSocket受信, LLM生成)は動きます
        await asyncio.to_thread(play_audio, wav_data, len(script) / 10.0)
        audio_queue.task_done()

async def main():
    # ノートを受け渡しするための非同期キュー
    note_queue = asyncio.Queue()
    
    # 生成された音声を再生待ちにしておくキュー。
    # maxsize=1とすることで、未来の台本を作りすぎるのを防ぎ、リアルタイム性を保ちます。
    audio_queue = asyncio.Queue(maxsize=1)
    
    # 3つのタスクを並行実行する
    task1 = asyncio.create_task(fetch_notes(note_queue))
    task2 = asyncio.create_task(radio_personality(note_queue, audio_queue))
    task3 = asyncio.create_task(audio_worker(audio_queue))
    
    # すべてのタスクが終了するまで待機（通常は無限ループになります）
    await asyncio.gather(task1, task2, task3)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")