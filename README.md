# PatentViewer

別端末・新しいCodex環境への本番移行は、最初に
[docs/ENVIRONMENT_MIGRATION_RUNBOOK.md](docs/ENVIRONMENT_MIGRATION_RUNBOOK.md)
を参照してください。SQLite保存方式は
[docs/SQLITE_STORAGE.md](docs/SQLITE_STORAGE.md)、従来の新規構築方針は
[docs/PRODUCTION_BOOTSTRAP.md](docs/PRODUCTION_BOOTSTRAP.md) にあります。

ローカルの `patent_pool/` を共有PDF格納庫として、リサーチ単位の特許マップ、PDFプレビュー、解釈メモ、人間とCodexの可視UI協働を提供するHTMLベースのPatentViewerです。

## 現在できること

- `researches/<research>/patent_list_{yyyymmddHHMMSS}.csv` のCP932直接取込（最新1件）
- CSVがないリサーチは直下の `patents.json` を入力として使用（旧サブリサーチ形式も読込互換）
- リサーチ／出願年範囲／権利化・審査中・公開／検索のリアルタイム絞り込み
- 脅威マップと技術マップ、セル連動文献一覧
- `patent_pool/` のPDFをUI内または別タブでプレビュー
- 文献ごとの分析要約、評価根拠、解釈メモ保存
- NORMAL/DEBUGの入力・成果物・監査保存先分離
- Codexが可視DOMを操作するUI Bridge（発見、意図、進捗、pause/resume/cancel、監査）
- リポジトリ同梱MCP、ルールベース文献検索、高水準UIブロック、dry-run、冪等実行、永続監査
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

OSに依存しない起動とCodex協働設定には、Pythonランチャーも使用できます。

```powershell
python tools/patent_viewer.py start
python tools/patent_viewer.py mcp-config --write
python tools/patent_viewer.py doctor
```

別デバイスではclone後に同じ3コマンドを実行します。デバイス固有のPythonパスとMCP設定は `.codex/mcp.local.json`、実行ごとの接続トークンは `runtime/server-control.json` に生成され、Gitには保存されません。UIだけを軽く使用する場合は `start --no-managed-ollama` を指定できます。詳細は [Codex collaboration設計](docs/CODEX_COLLABORATION.md) を参照してください。

本番端末を構築する場合は、[環境移行・本番初回構築ランブック](docs/ENVIRONMENT_MIGRATION_RUNBOOK.md) を使用します。[本番端末の新規構築手順](docs/PRODUCTION_BOOTSTRAP.md) はデータ境界の要約です。GitにはNORMALリサーチ入力を含めず、開発端末のPDF、DB、runtime、分析結果も本番へ移行しません。

ローカルLLM検証を含むPython依存パッケージは次で導入できます。

```powershell
python -m pip install -r requirements.txt
```

## DEBUGテスト

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\debug.ps1
```

このコマンドはunit、API、NORMAL/DEBUG分離、Bridgeプロトコル契約、合成DEBUG fixture、wide/narrow画像回帰、可視ブラウザの全Semantic Block smokeを検査します。可視ブラウザが接続されていない場合は成功扱いにせず停止します。ブラウザ確認だけを明示的に省略する場合は `python tools/debug_check.py --allow-browser-skip` を使用します。

画像基準を意図的なUI変更に合わせて更新する場合だけ、`python tools/ui_visual_check.py --update-baselines` を実行してください。通常実行は `tests/visual_baselines/` と比較し、終了時に対象ブラウザを元のNORMAL/DEBUG環境へ戻します。複数ブラウザが接続されている場合、`tools/browser_smoke.py --client-id <id>` で対象を明示します。

1. `DEBUG: レーザー加工デモ` が表示される。
2. 出願年の開始・終了、権利化・審査中・公開、検索条件で件数と両マップが即時変わる。
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

実PDFを使う単一文献検証データはGitに含めません。`debug_data/researches/` にローカル検証用リサーチを作成し、対応PDFを `patent_pool/` に配置してから、リサーチIDを明示して実行します。再実行は既存結果を保護するため既定で拒否されます。

```powershell
python tools\run_single_llm_test.py --research-id <debug_research_id>
```

処理はPDF抽出、`gemma4:e4b` の構造化分析、`qwen3-embedding:8b` の2要約Embedding、型・値域・空応答・次元検証、原子的な結果確定の順です。`gemma4:e4b` はOllamaのJSON Schema grammarを受理しないため、モデルはJSONモードで実行し、より厳密なSchemaをPython側で検証します。

成果物:

- UI用結果: `debug_data/researches/<debug_research_id>/results/<patent-id>.json`
- 実行監査: 同フォルダ階層の `runs/<run-id>/run_manifest.json`
- 抽出情報・テキスト・プロンプト・生成応答・Embedding応答: 同じrunフォルダ

結果を意図的に置換する場合だけ `--overwrite` を付けます。通常運用データへ使う前に、生成要約と請求項本文を人が比較してください。

## 段階分析パイプライン

通常の複数文献分析は、PDF抽出と章・請求項分割をPythonで行った後、ローカルLLMへ「類似度」「権利範囲の広さ」「課題要約」「技術要約」の4つの小さなJSONだけを個別に要求します。各要求には専用の小さなJSON Schemaを適用し、Ollamaの生成制約に加えてPythonでも型、必須キー、追加キー、値域、文字数を再検証します。その後、自社技術定義を技術・対象課題の2要約へ一度だけ正規化し、文献と自社のEmbedding、リサーチ内クラスタ、短いクラスタ名、コサイン距離による意味順と自社近接度を確定します。

前処理ではページ・段落ID、章と役割、重複・定型句、請求項の構成要件と限定をルールベースで構造化します。LLMへ渡す文字数上限は変えず、4タスクごとに関連性の高い原文だけを選んだEvidence Packを生成します。元の抽出全文と採用根拠は保持され、後段のLLM・Embedding・クラスタリング契約は従来どおりです。

文献別の生成LLMは通常、従来どおり4回です。類似度と権利範囲の広さは根拠となる構成要件ID、課題要約は根拠段落ID、技術要約は構成要件または段落IDを小さな配列で返します。Pythonは各IDがそのタスクのEvidence Packへ実際に含まれていたかを検証します。不一致なら、そのタスクだけ候補IDを限定して1回自動再生成するため、その文献は5回以上になることがあります。再生成も不一致なら文献を失敗扱いにし、検証過程とrun単位の検証率を記録します。

```powershell
python tools/run_research_pipeline.py <research_id> --stage plan
python tools/run_research_pipeline.py <research_id> --stage prepare
python tools/run_research_pipeline.py <research_id> --stage execute
```

共有抽出キャッシュは `runtime/shared/extractions/` に置きます。抽出全文・ページ別本文に加え、PDFだけで決まる章・段落・請求項・構成要件の構造化結果もPDFハッシュ単位で共有します。自社技術や読取方針に依存するEvidence Packと分析結果はリサーチ別に保存します。元PDFを常に正とし、リサーチ／文献ごとに再抽出や全文読解を選べます。脅威マップは従来どおり「自社技術との類似度 × 権利範囲の広さ」の5×5です。詳細は [docs/RESEARCH_PIPELINE_DESIGN.md](docs/RESEARCH_PIPELINE_DESIGN.md) を参照してください。

同じ操作は画面上部の `夜間一括分析` から、Codexを介さず実行できます。`全件を再分析`を選べば既存結果を手動削除せずに全PDFを更新できます。アプリは既存の11434番Ollamaに干渉せず、空きローカルポートで専用Ollamaを起動・監視・終了します。GPUの総VRAMと空きVRAMから生成並列数とEmbeddingバッチ数を自動決定し、短文タスク、長文タスク、Embeddingの順にまとめてモデル再ロードを抑えます。UIでは冷却時間を0〜180秒（既定0）で指定でき、現在工程、工程経過、総経過、推定残り、工程別実績を確認できます。冷却待機中もモデルはVRAMに保持します。文献別の `analysis_progress.json` に完了タスクを保存するため、停止後は文献全体ではなく未完了タスクから再開します。1文献のJSON生成・Schema検証・Embedding検証に失敗した場合は、その文献だけを失敗として記録して次文献へ進みます。要求と応答は文献別の `attempts/<run-id>/llm_calls/`、最新失敗は `analysis_error.json`、run全体の設定・失敗・スキップ一覧は `run_manifest.json` に保存します。pause/resume/cancelが使用でき、実行中はブラウザが閉じてもサーバーのアイドル終了を抑止します。

大規模リサーチでは、全ペア距離を保持する階層クラスタリングを使用せず、正規化・固定射影・決定的MiniBatch cosine K-meansへ自動的に切り替えます。クラスタ名生成は各クラスタから最大20件の要約を使い、16Kコンテキストを超えないよう制限します。

通常はVRAM自動設定を使用します。検証時だけ上書きする場合は、アプリサーバーへ `--generation-workers 1`、`--embedding-batch-size 32` のように指定できます。指定しなければGPU名ではなく、その起動時点の総VRAMと空きVRAMから決定します。GPUが20GB以上かつCPUメモリが64GB以上なら本番向け永続シャードモードとなり、500件ずつ分析・Embeddingを確定してメモリを解放し、全シャード完了後に保存済みEmbeddingを再読込してリサーチ全体を一度だけクラスタリングします。それ未満の環境では従来どおり一括フローを使用します。採用値は `/api/health`、pipeline job、`run_manifest.json` に記録されます。

PDFテキスト抽出と章・請求項構造化だけを先に行う場合は、画面でリサーチを選び、`夜間一括分析` を開いて `前処理だけ実行` を押します。この操作ではOllamaによる意味分析を実行しません。

## リサーチ追加

画面の `リサーチ管理` から、名前、ID、説明、自社技術、`patent_list_{yyyymmddHHMMSS}.csv` を登録する。CSVはCP932・既定11列を事前検証し、PDF一致・未発見・複数候補・警告件数を表示してから保存する。作成された `researches/<research_id>/` は本番運用データであり、Gitには追加しない。

同じリサーチへCSVを追加した場合、ファイル名のタイムスタンプが最新の1件だけを採用し、旧CSVは履歴として保持する。最新版が切り替わると分析状態は「再分析必要」となり、夜間一括分析で全件再分析を完了するまで解除されない。夜間一括分析画面では使用中リサーチを選択できる。

リサーチ管理からアーカイブすると通常一覧と夜間分析対象から外れるが、CSV、分析結果、実行履歴は保持される。復元すると再び使用できる。

分析結果は `results/<正規化文献ID>.json`、段階成果物は `pipeline/<正規化文献ID>/` へ自動保存される。

PDFそのものをリサーチフォルダへ複製しないことが重要です。

本番リサーチフォルダに格納される `patent_list_{yyyymmddHHMMSS}.csv` の列仕様と現行の取込動作は [docs/PATENT_LIST_CSV_SPEC.md](docs/PATENT_LIST_CSV_SPEC.md) に記録しています。

PDFファイル名は従来の `JPA ...` / `JPB ...` に加え、`WO20xx-XXXXXX`、`特開20xx-XXXXXX`、`特表20xx-XXXXXX`、`特許第XXXXXXX号`、`特開平x-XXXXXX`、`特表平x-XXXXXX` を使用できます。全角数字と一般的なハイフン表記も正規化します。CSV入力の年次フィルタは「出願日」の年を使用し、CSVを使わない互換データでは `patents.json` の `year` または公報番号からの推定値を使用します。

## Git管理するデータの境界

GitHubには、再現可能なアプリ本体、テスト、スキーマ、プロンプト、セットアップ資料だけを保存します。NORMALリサーチのCSV、自社技術定義、分析結果は保存しません。`debug_laser_demo` は架空の名称・評価を使ったDEBUG環境の固定回帰fixtureなので、例外的にコミット対象です。

次のローカル資産・生成物は `.gitignore` で除外します。

- `patent_pool/` の特許明細書PDF（ディレクトリを保持する `.gitkeep` のみ管理）
- `researches/` のNORMALリサーチ入力、自社技術定義、CSV、全生成物（ディレクトリを保持する `.gitkeep` のみ管理）
- `old_source/` と、構築時に参照した旧仕様・旧UI設計文書
- 固定fixture以外の `debug_data/researches/` 入力と生成物
- 実PDF検証の抽出本文、プロンプト、Ollama応答、Embedding、実行監査
- `runtime/`、ログ、Pythonキャッシュ、一時ファイル

DEBUG用の固定フィクスチャと、LLM実行で生成されるデバッグ成果物は区別します。前者は回帰確認に必要なため管理し、後者はPDF由来の本文を含み得るためローカルにだけ保存します。
