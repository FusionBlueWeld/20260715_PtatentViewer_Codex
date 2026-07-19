# PatentViewer

ローカルの `patent_pool/` を共有PDF格納庫として、リサーチ単位の特許マップ、PDFプレビュー、解釈メモ、人間とCodexの可視UI協働を提供するHTMLベースのPatentViewerです。

## 現在できること

- `researches/<research>/patent_list_{yyyymmddHHMMSS}.csv` のCP932直接取込（最新1件）
- CSVがない既存リサーチは `subresearches/<subresearch>/patents.json` を互換入力として使用
- リサーチ／サブリサーチ／年次／公開・登録／検索のリアルタイム絞り込み
- 脅威マップと技術マップ、セル連動文献一覧
- `patent_pool/` のPDFをUI内または別タブでプレビュー
- 文献ごとの分析要約、評価根拠、解釈メモ保存
- NORMAL/DEBUGの入力・成果物・監査保存先分離
- Codexが可視DOMを操作するUI Bridge（発見、意図、進捗、pause/resume/cancel、監査）
- モデル推論を実行しないLLM preflight

## 起動

エクスプローラーで次のファイルをダブルクリックします。

- `scripts/start.bat`: サーバーをバックグラウンド起動し、PatentViewerのURLを1つだけ開く
- `scripts/stop.bat`: トークン認証されたlocalhost終了APIでサーバーを安全に停止する

起動時はPython 3.10以上の候補から、必要なパッケージが導入済みの環境を自動選択します。どの候補にも不足がある場合は、`requirements.txt` のパッケージを初回起動時に導入します。

起動済みの状態で `scripts/start.bat` を再実行すると、サーバーは重複起動せずPatentViewer画面だけを開きます。Edge（なければChrome）のアプリモードを横長サイズで起動するため、余分な「新しいタブ」は作りません。

既定では、人間またはCodexによる実操作が30分間なければ自動停止します。Bridgeのheartbeatやコマンド待受poll、ヘルスチェックだけでは利用時間を延長しません。

PowerShellから条件を変更する場合:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -IdleTimeoutMinutes 30
```

ブラウザを開かずサーバーだけ起動する場合は `-NoBrowser`、ポート変更は `-Port 8799` を指定します。起動情報は `runtime/server-control.json`、ログは `runtime/server.stdout.log` と `runtime/server.stderr.log` に保存されます。

ブラウザで `http://127.0.0.1:8765` を開きます。サーバーは既定でlocalhostにだけbindします。

ローカルLLM検証を含むPython依存パッケージは次で導入できます。

```powershell
python -m pip install -r requirements.txt
```

## DEBUGテスト

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\debug.ps1
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

PDF選択、小型プロンプト5件、小型JSON Schema 5件、出力分離、Ollama接続、必要モデル、請求項構造を検査します。診断中にモデル推論は行いません。不足があれば `blocked`、全条件が揃えば `ready` を返します。LLM別Schemaは `schemas/llm-*.schema.json`、プロンプトは `src/patent_viewer/prompts/stages/` にあります。

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

## 段階分析パイプライン

通常の複数文献分析は、PDF抽出と章・請求項分割をPythonで行った後、ローカルLLMへ「類似度」「権利範囲の広さ」「課題要約」「技術要約」の4つの小さなJSONだけを個別に要求します。各要求には専用の小さなJSON Schemaを適用し、Ollamaの生成制約に加えてPythonでも型、必須キー、追加キー、値域、文字数を再検証します。その後、2要約のEmbedding、リサーチ内クラスタ、短いクラスタ名生成を実行します。

```powershell
python tools/run_research_pipeline.py laser_process_landscape --stage plan
python tools/run_research_pipeline.py laser_process_landscape --stage prepare
python tools/run_research_pipeline.py laser_process_landscape --stage execute
```

共有抽出キャッシュは `runtime/shared/extractions/` に置きますが、元PDFを常に正とし、リサーチ／文献ごとに再抽出や全文読解を選べます。脅威マップは従来どおり「自社技術との類似度 × 権利範囲の広さ」の5×5です。詳細は [docs/RESEARCH_PIPELINE_DESIGN.md](docs/RESEARCH_PIPELINE_DESIGN.md) を参照してください。

同じ操作は画面上部の `夜間一括分析` から、Codexを介さず実行できます。アプリは既存の11434番Ollamaに干渉せず、空きローカルポートで専用Ollamaを起動・監視・終了します。GPUの総VRAMと空きVRAMから生成並列数、Embeddingバッチ数、冷却間隔を自動決定し、短文タスク、長文タスク、Embeddingの順にまとめてモデル再ロードを抑えます。冷却待機中もモデルはVRAMに保持し、生成完了後に一度だけEmbeddingモデルへ切り替えます。文献別の `analysis_progress.json` に完了タスクを保存するため、停止後は文献全体ではなく未完了タスクから再開します。1文献のJSON生成・Schema検証・Embedding検証に失敗した場合は、その文献だけを失敗として記録して次文献へ進みます。要求と応答は文献別の `attempts/<run-id>/llm_calls/`、最新失敗は `analysis_error.json`、run全体の設定・失敗・スキップ一覧は `run_manifest.json` に保存します。pause/resume/cancelが使用でき、実行中はブラウザが閉じてもサーバーのアイドル終了を抑止します。

大規模リサーチでは、全ペア距離を保持する階層クラスタリングを使用せず、正規化・固定射影・決定的MiniBatch cosine K-meansへ自動的に切り替えます。クラスタ名生成は各クラスタから最大20件の要約を使い、16Kコンテキストを超えないよう制限します。

通常はVRAM自動設定を使用します。検証時だけ上書きする場合は、アプリサーバーへ `--generation-workers 1`、`--embedding-batch-size 32` のように指定できます。指定しなければGPU名や16/24GBラベルではなく、その起動時点の総VRAMと空きVRAMから決定します。採用値は `/api/health`、pipeline job、`run_manifest.json` に記録されます。

PDFテキスト抽出と章・請求項構造化だけを先に行う場合は、画面でリサーチを選び、`夜間一括分析` を開いて `前処理だけ実行` を押します。この操作ではOllamaによる意味分析を実行しません。

## リサーチ追加

1. `researches/<research-id>/` を作る。表示名や個別パイプライン設定が必要な場合だけ `research.json` を置く。
2. リサーチ直下に `patent_list_{yyyymmddHHMMSS}.csv` と `company_tech.txt` を置く。CSVが複数ある場合はファイル名の最新タイムスタンプ1件だけを使う。
3. 分析結果は `subresearches/patent_list/results/<正規化文献ID>.json` へ自動保存される。

PDFそのものをリサーチフォルダへ複製しないことが重要です。

本番リサーチフォルダに格納される `patent_list_{yyyymmddHHMMSS}.csv` の列仕様と現行の取込動作は [docs/PATENT_LIST_CSV_SPEC.md](docs/PATENT_LIST_CSV_SPEC.md) に記録しています。

PDFファイル名は従来の `JPA ...` / `JPB ...` に加え、`WO20xx-XXXXXX`、`特開20xx-XXXXXX`、`特表20xx-XXXXXX`、`特許第XXXXXXX号`、`特開平x-XXXXXX`、`特表平x-XXXXXX` を使用できます。全角数字と一般的なハイフン表記も正規化します。平成表記は年次フィルタ用に西暦へ変換し、登録特許のファイル名だけでは年次を確定できないため、必要な場合は `patents.json` の `year` に明示してください。

## Git管理するデータの境界

GitHubには、再現可能なアプリ本体、テスト、スキーマ、プロンプト、リサーチ参照リストを保存します。`debug_laser_demo` はDEBUG環境をすぐ検証できる固定フィクスチャなのでコミット対象です。

次のローカル資産・生成物は `.gitignore` で除外します。

- `patent_pool/` の特許明細書PDF（ディレクトリを保持する `.gitkeep` のみ管理）
- `old_source/` と、構築時に参照した旧仕様・旧UI設計文書
- 通常リサーチの `results/`、`runs/`
- `debug_llm_single` の抽出本文、プロンプト、Ollama応答、embedding、実行監査
- `runtime/`、ログ、Pythonキャッシュ、一時ファイル

DEBUG用の固定フィクスチャと、LLM実行で生成されるデバッグ成果物は区別します。前者は回帰確認に必要なため管理し、後者はPDF由来の本文を含み得るためローカルにだけ保存します。
