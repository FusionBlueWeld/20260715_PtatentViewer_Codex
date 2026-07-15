# PatentViewer

ローカルの `patent_pool/` を共有PDF格納庫として、リサーチ単位の特許マップ、PDFプレビュー、解釈メモ、人間とCodexの可視UI協働を提供するHTMLベースのPatentViewerです。

## 現在できること

- `researches/<research>/subresearches/<subresearch>/patents.json` によるPDF参照
- リサーチ／サブリサーチ／年次／公開・登録／検索のリアルタイム絞り込み
- 脅威マップと技術マップ、セル連動文献一覧
- `patent_pool/` のPDFをUI内または別タブでプレビュー
- 文献ごとの分析要約、評価根拠、解釈メモ保存
- NORMAL/DEBUGの入力・成果物・監査保存先分離
- Codexが可視DOMを操作するUI Bridge（発見、意図、進捗、pause/resume/cancel、監査）
- Ollamaを起動しないLLM preflight

## 起動

エクスプローラーで次のファイルをダブルクリックします。

- `scripts/start.bat`: サーバーをバックグラウンド起動し、PatentViewerのURLを1つだけ開く
- `scripts/stop.bat`: トークン認証されたlocalhost終了APIでサーバーを安全に停止する

起動済みの状態で `scripts/start.bat` を再実行すると、サーバーは重複起動せずPatentViewer画面だけを開きます。Edge（なければChrome）のアプリモードを横長サイズで起動するため、余分な「新しいタブ」は作りません。

既定では、人間またはCodexによる実操作が30分間なければ自動停止します。Bridgeのheartbeatやコマンド待受poll、ヘルスチェックだけでは利用時間を延長しません。

PowerShellから条件を変更する場合:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -IdleTimeoutMinutes 30
```

ブラウザを開かずサーバーだけ起動する場合は `-NoBrowser`、ポート変更は `-Port 8799` を指定します。起動情報は `runtime/server-control.json`、ログは `runtime/server.stdout.log` と `runtime/server.stderr.log` に保存されます。

ブラウザで `http://127.0.0.1:8765` を開きます。サーバーは既定でlocalhostにだけbindします。

ローカルLLM検証を含むPython依存パッケージは次で導入できます。

```powershell
python -m pip install -r requirements.txt
```

## DEBUGテスト

```powershell
powershell -ExecutionPolicy Bypass -File .\debug.ps1
```

このコマンドはunit、API、NORMAL/DEBUG分離、Bridgeプロトコル契約、実データマニフェストを検査します。その後UIをDEBUGへ切り替え、次を実ブラウザで確認します。

1. `DEBUG: レーザー加工デモ` が表示される。
2. 年次、公開・登録、サブリサーチで件数と両マップが即時変わる。
3. マップセルから文献を選び、PDFモーダルを開ける。
4. Codex collaborationパネルが表示され、可視targetが `/api/ui/clients` に現れる。
5. テスト終了時にNORMALへ戻す。

DEBUGの入力は `debug_data/`、書込みは `runtime/debug/` です。NORMALは `researches/` と `runtime/normal/` を使います。`patent_pool/` は両環境で共有しますが、読み取り専用です。

## ローカルLLM接続前

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -X utf8 tools\llm_preflight.py debug_laser_demo --environment debug
```

PDF選択、プロンプト、出力分離を検査します。現段階ではOllamaと必要モデルを意図的に `blocked` として報告し、モデル呼出しは行いません。結果形式は `schemas/analysis-result.schema.json`、プロンプトは `src/patent_viewer/prompts/` にあります。

まず少数のDEBUG文献で抽出・分析品質を承認した後、通常リサーチへ進める設計です。工程・受入条件は [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) を参照してください。

## 実PDF 1件のOllama検証

`DEBUG: ローカルLLM 1件実証` は `JPA 2026066788-000000.pdf` だけを対象にします。再実行は既存結果を保護するため既定で拒否されます。

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -X utf8 tools\run_single_llm_test.py
```

処理はPDF抽出、`gemma4:e4b` の構造化分析、`qwen3-embedding:8b` の2要約Embedding、型・値域・空応答・次元検証、原子的な結果確定の順です。`gemma4:e4b` はOllamaのJSON Schema grammarを受理しないため、モデルはJSONモードで実行し、より厳密なSchemaをPython側で検証します。

成果物:

- UI用結果: `debug_data/researches/debug_llm_single/subresearches/single_document/results/<patent-id>.json`
- 実行監査: 同フォルダ階層の `runs/<run-id>/run_manifest.json`
- 抽出情報・テキスト・プロンプト・生成応答・Embedding応答: 同じrunフォルダ

結果を意図的に置換する場合だけ `--overwrite` を付けます。通常運用データへ使う前に、生成要約と請求項本文を人が比較してください。

## リサーチ追加

1. `researches/<research-id>/research.json` を作る。
2. `subresearches/<subresearch-id>/patents.json` に `patent_pool/` 内のPDFファイル名を列挙する。
3. LLM接続後は同じサブリサーチの `results/<PDF stemの空白を_にしたID>.json` へ分析結果を保存する。

PDFそのものをリサーチフォルダへ複製しないことが重要です。

## Git管理するデータの境界

GitHubには、再現可能なアプリ本体、テスト、スキーマ、プロンプト、リサーチ参照リストを保存します。`debug_laser_demo` はDEBUG環境をすぐ検証できる固定フィクスチャなのでコミット対象です。

次のローカル資産・生成物は `.gitignore` で除外します。

- `patent_pool/` の特許明細書PDF（ディレクトリを保持する `.gitkeep` のみ管理）
- `old_source/` と、構築時に参照した旧仕様・旧UI設計文書
- 通常リサーチの `results/`、`runs/`
- `debug_llm_single` の抽出本文、プロンプト、Ollama応答、embedding、実行監査
- `runtime/`、ログ、Pythonキャッシュ、一時ファイル

DEBUG用の固定フィクスチャと、LLM実行で生成されるデバッグ成果物は区別します。前者は回帰確認に必要なため管理し、後者はPDF由来の本文を含み得るためローカルにだけ保存します。
