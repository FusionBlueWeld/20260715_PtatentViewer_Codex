# PatentViewer 環境移行・本番初回構築ランブック

更新日: 2026-07-26

## 1. この文書の位置づけ

この文書は、PatentViewerを初めて置く端末で、コード取得から本番分析開始までを再現するための正本である。移行先の担当者またはCodexは、作業前にこの文書を最後まで読み、各Phaseの完了条件を満たしてから次へ進むこと。

「初めての環境で走らせるとき」は、必ず次の三つを構築する。

1. **環境台帳**: OS、Python、GPU、Ollama、モデル、Git revision、保存先、容量を記録する。
2. **移行台帳**: Gitで移すもの、別経路で配置するPDF・CSV、移してはいけないDB・生成物を記録する。
3. **受入記録**: テスト、SQLite検証、API、ブラウザ、少数文献分析の結果を記録する。

文書と実装が食い違う場合は、実際のコードとコマンドのヘルプを確認し、本番データ投入前に文書または実装を修正する。存在しないコマンドやテーブルを推測で作らない。

関連資料:

- [README](../README.md)
- [本番端末の新規構築手順](PRODUCTION_BOOTSTRAP.md)
- [SQLite保存方式](SQLITE_STORAGE.md)
- [本番CSV仕様](PATENT_LIST_CSV_SPEC.md)
- [分析パイプライン設計](RESEARCH_PIPELINE_DESIGN.md)
- [Codex collaboration](CODEX_COLLABORATION.md)

## 2. 移行先Codexへの最初の指示

新しいCodex環境では、cloneしたプロジェクトフォルダをワークスペースとして開き、次の短い依頼を行える。

> このシステムを実行できる環境を立ち上げてください。

ルートの `AGENTS.md` は、この依頼を本ランブックの全初回構築として解釈する。Codexが `AGENTS.md` を自動読込しない環境、または対象範囲をより明確に伝えたい場合は、次をそのまま指示する。

> このリポジトリの `docs/ENVIRONMENT_MIGRATION_RUNBOOK.md` を最後まで読み、Phase 0から順番に実施してください。各Phaseの完了条件を確認し、環境台帳・移行台帳・受入記録を作成してください。`runtime/`、SQLite DB、旧分析結果、端末固有MCP設定を開発端末からコピーしないでください。コマンドは現在の実装と `--help` を照合し、Git revisionが確定していない、PDFの利用許可がない、CSVとPDFの対応が確認できない、LLM実行の承認がない場合は、そのPhaseで停止して状況を報告してください。

Codexは次の規則を守る。

- リポジトリルートを確認してからコマンドを実行する。
- 端末固有の絶対パスをGit追跡ファイルへ書かない。
- PDF、抽出本文、LLM要求・応答をチャットや外部サービスへ送信しない。
- 大量分析を開始する前に、対象件数、モデル、推定負荷、停止・再開方法を提示する。
- `runtime/server-control.json` のトークンを表示・共有・コミットしない。
- SQLite DBをネットワーク共有ドライブ上で運用しない。
- 稼働中のSQLite DBを単純コピーしない。

## 3. 本番化の前提とデータ移行方針

### 3.1 推奨方針

本番端末へ移す正本は、確定したGit commitまたはrelease tagである。次はコピーしない。

- `runtime/` 全体
- `runtime/normal/patent_viewer.sqlite3`
- `runtime/debug/patent_viewer.sqlite3`
- `runtime/server-control.json`
- `.codex/mcp.local.json`
- 開発端末で生成した `results/`、`runs/`、`pipeline/`、`clustering/`
- 開発端末の抽出キャッシュ、Embedding、LLM要求・応答、ログ

本番PDFはGitでは運ばない。利用権限を確認したうえで、承認された保管元から本番端末の `patent_pool/` へ別経路で配置する。分析結果は本番端末で作り直す。

### 3.2 releaseに含まれるリサーチ入力

GitのreleaseにはNORMALリサーチの入力を含めない。`researches/` は `.gitkeep` だけを追跡し、次はすべて本番端末のローカル運用データとして扱う。

- `research.json`
- `company_tech.txt`
- `patent_list_*.csv`
- `organization_overrides.json`
- `results/`, `runs/`, `pipeline/`, `clustering/`

したがって、新規clone後の初回SQLite同期はNORMALリサーチ0件・文献0件が正しい。本番リサーチは、承認されたPDF、CSV、自社技術定義を本番端末へ配置してからUIで新規作成する。開発・検証端末のリサーチフォルダをコピーしたり、Gitへ追加したりしない。

### 3.3 Git管理境界

| 種類 | Git | 本番への渡し方 |
|---|---:|---|
| `src/`, `public/`, `tools/`, `scripts/`, `schemas/`, `docs/`, `tests/` | 対象 | release commit/tag |
| 合成DEBUG固定fixture | 対象 | release commit/tag |
| NORMALリサーチ入力・自社技術・採用CSV | 対象外 | 本番端末で新規作成 |
| 固定fixture以外のDEBUG入力 | 対象外 | 検証端末でローカル作成 |
| `patent_pool/*.pdf` | 対象外 | 承認済み保管元から別経路 |
| SQLite DB、`runtime/` | 対象外 | 移行せず本番で生成 |
| `results/`, `runs/`, `pipeline/`, `clustering/` | 対象外 | 移行せず本番で生成 |
| `.codex/mcp.local.json` | 対象外 | 本番端末で生成 |
| 企業グループ共通設定 | 対象外 | `config/organization_registry.json` を本番資産としてバックアップ |
| リサーチ別企業グループ | 対象外 | `organization_overrides.json` をリサーチと一緒にバックアップ |

## 4. 現在の検証済み環境スナップショット

以下は2026-07-26時点の開発・検証端末の実測値であり、新端末の絶対要件ではない。新端末では同じ項目を実測し、差分を環境台帳へ記録する。この節は互換性と容量計画の参考情報だけを扱い、リサーチID、CSV、特許番号、分析内容、生成結果は参照しない。

### 4.1 OS・実行基盤

| 項目 | 現在値 |
|---|---|
| OS | Microsoft Windows NT 10.0.26200.0 |
| アーキテクチャ | AMD64 |
| PowerShell | 5.1.26100.8894 |
| CPU | Intel Core i7-14700F |
| RAM | 15.8 GiB |
| GPU | NVIDIA GeForce RTX 4070 Ti SUPER |
| GPU driver | 595.95 |
| VRAM | 16,376 MiB |
| Git | 2.47.1.windows.1 |
| Python | 3.12.2 |
| Node.js | 22.17.0。開発時のJS構文確認用で、通常運用の必須条件ではない |
| ブラウザ | Microsoft EdgeまたはGoogle Chrome |
| 既定UI | `http://127.0.0.1:8765/` |
| アプリ版 | 0.2.0 |
| Collaboration protocol | 3 |
| SQLite schema | 1 |

### 4.2 Python依存の検証済み組合せ

`requirements.txt` の範囲内で、現在は次を使用している。

| パッケージ | 現在値 |
|---|---:|
| pypdf | 6.14.2 |
| numpy | 2.4.6 |
| Pillow | 12.2.0 |
| playwright | 1.61.0 |

通常は `requirements.txt` を使用する。移行先で異なるバージョンが選ばれた場合は、受入記録に実バージョンを残す。

### 4.3 Ollamaとモデル

| 項目 | 現在値 |
|---|---|
| Ollama | 0.32.3 |
| 生成モデル | `gemma4:e4b` / ID `c6eb396dbd59` / 約9.6 GB |
| rescueモデル | `gpt-oss:20b` / ID `17052f91a42e` / 約13 GB |
| Embeddingモデル | `qwen3-embedding:8b` / ID `64b933495768` / 約4.7 GB |

パイプラインの通常契約は、生成 `gemma4:e4b`、rescue `gpt-oss:20b`、Embedding `qwen3-embedding:8b` である。モデル名だけでなく、移行先の `ollama list` に表示されるIDも記録する。

### 4.4 現在の自動リソース設定

`/api/health` の実測値:

| 項目 | 現在値 |
|---|---:|
| mode | `auto` |
| GPU free VRAM | 14,925 MiB |
| reserved VRAM | 2,456 MiB |
| generation workers | 1 |
| embedding batch size | 32 |
| shard size | 250 |
| cooldown every documents | 50 |
| durable shards | `false` |
| max loaded models | 1 |
| keep alive | `1h` |

20,000 MiB以上のVRAMかつ64 GiB以上のRAMがある場合だけ、永続シャードモードが自動選択される。現在端末はこの条件を満たさない。大規模本番処理では、24 GB級以上のGPUと64 GB以上のRAMを推奨し、実際の採用値は必ず `/api/health` と `run_manifest.json` で確認する。

### 4.5 releaseのNORMALデータ境界

releaseにはNORMALの入力・DB・分析結果を含めない。新規clone直後の期待値は次のとおり。

| 項目 | 期待値 |
|---|---:|
| リサーチ | 0 |
| 文献 | 0 |
| PDF | 0 |
| Embedding | 0 |
| artifact records | 0 |

開発・検証端末の件数やSQLite実測値はrelease資料へ固定せず、各端末の環境台帳・受入記録にだけ保存する。

## 5. Phase 0: 開発端末で本番releaseを確定する

### 5.1 完了必須項目

- [ ] 本番へ含める機能がすべてコミット対象になっている
- [ ] `git status --short` が空である
- [ ] unit/APIテストが成功する
- [ ] JavaScript構文検査が成功する
- [ ] SQLite検証が成功する
- [ ] ブラウザで主要UIを確認する
- [ ] 本番用commitまたはtagを作る
- [ ] commit、tag、作成日時を移行台帳へ記録する

本番では、データ境界の確認と検証を完了したcommitまたはrelease tagを使用する。文書へ固定された過去のcommit SHAを本番revisionとして流用しない。

確認コマンド:

```powershell
git status --short
git diff --check
node --check public/assets/app.js
python -m unittest discover -s tests -v
python tools/migrate_to_sqlite.py --environment normal --verify-only --json
git rev-parse HEAD
git describe --tags --always --dirty
```

`git describe` に `-dirty` が付いた状態で本番へ渡さない。

## 6. Phase 1: 移行先の環境台帳を作る

### 6.1 必須ソフトウェア

- [ ] 64-bit Windows
- [ ] Git
- [ ] Python 3.10以上。現在の検証版は3.12.2
- [ ] `pip`
- [ ] EdgeまたはChrome
- [ ] Ollama
- [ ] NVIDIA driverと `nvidia-smi`。GPUなしでもfallback動作するが、大量分析には非推奨
- [ ] 十分なローカルディスク
- [ ] Codexからこのリポジトリを操作する場合は、repo-local MCPを読み込めるCodex環境

PowerShellで次を実行し、結果を環境台帳へ貼る。

```powershell
[Environment]::OSVersion.VersionString
$PSVersionTable.PSVersion
git --version
python --version
python -m pip --version
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free --format=csv,noheader
ollama --version
ollama list
Get-PSDrive -Name C
```

`python` がPATHにない場合でも `scripts/start.ps1` は、次の順でPython 3.10以上を探す。

1. `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`
2. `%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
3. PATH上の `python`

本番運用では、Codex内蔵ランタイムだけに依存せず、端末管理下のPython 3.12系を用意するのが望ましい。

PATHにPythonを追加しない運用では、構築セッションの最初に次で実行ファイルを確定する。

```powershell
$pythonCandidates = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
  (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$pathPython = Get-Command python -ErrorAction SilentlyContinue
if ($pathPython) { $pythonCandidates += $pathPython.Source }
$pythonExe = $pythonCandidates |
  Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
  Select-Object -Unique |
  Select-Object -First 1
if (-not $pythonExe) { throw 'Python 3.10以上が見つかりません。' }
& $pythonExe -X utf8 --version
```

以降の例にある `python ...` は、PATHにPythonがない場合は
`& $pythonExe -X utf8 ...` と読み替える。`scripts/start.ps1` はこの変数がなくても独自にPythonを選択する。

### 6.2 ハードウェア判断

- UI、検索、SQLiteだけならGPUは必須ではない。
- ローカルLLM分析にはNVIDIA GPUを推奨する。
- 現行の16 GB VRAM / 16 GB RAMでは、自動設定は原則1 worker、Embedding batch 32、durable shardsなしになる。
- 大量文献では24 GB級以上のVRAMと64 GB以上のRAMを推奨する。
- 必要ディスク量はPDF総量、抽出本文、文献別attempt、Embedding、実行履歴に依存する。全件投入前に10〜20件で生成量を測り、文献数へ外挿して空き容量を確保する。
- SQLite DBはローカルSSDへ置く。OneDrive同期フォルダやネットワーク共有上で直接運用しない。

## 7. Phase 2: コードを固定revisionで取得する

永続的に使用するローカルパスへcloneする。後からフォルダを移動すると `.codex/mcp.local.json` の絶対パスが無効になる。

```powershell
git clone <REPOSITORY_URL> <DESTINATION_DIRECTORY>
Set-Location -LiteralPath <DESTINATION_DIRECTORY>
git fetch --tags
git checkout <RELEASE_TAG_OR_COMMIT>
git rev-parse HEAD
git status --short
```

完了条件:

- [ ] `git rev-parse HEAD` が移行台帳のrelease revisionと一致する
- [ ] `git status --short` が空
- [ ] `.gitignore` に `runtime/`、`patent_pool/*`、生成結果、`.codex/mcp.local.json` が含まれる
- [ ] リポジトリがローカル固定ディスク上にある

## 8. Phase 3: Python依存とモデルを準備する

### 8.1 Python

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import pypdf,numpy,PIL,playwright; print('Python dependencies OK')"
python -m pip show pypdf numpy Pillow playwright
```

端末の変更管理上、`pip` の自動更新が禁止されている場合は1行目を省略し、承認済みpipを使う。

### 8.2 Ollama

```powershell
ollama pull gemma4:e4b
ollama pull gpt-oss:20b
ollama pull qwen3-embedding:8b
ollama list
```

モデルIDを環境台帳へ記録する。移行先でモデルIDが現在環境と異なる場合は、少数文献の品質確認を必須とする。

PatentViewerは起動時に既存の11434番Ollamaを再利用せず、空きlocalhostポートで専用Ollamaを起動する。モデルファイルは通常のOllamaモデルストアを共有する。`PATENT_VIEWER_OLLAMA_EXE` を設定しない場合は、PATHまたは `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` を探索する。

## 9. Phase 4: PDFとリサーチ入力を配置する

### 9.1 本番PDFを配置する

1. 利用権限を確認した本番PDFだけを `patent_pool/` 直下へ配置する。
2. PDF拡張子は `.pdf` とし、ファイル名はCSV照合規則に従う。
3. ファイル数だけでなくSHA-256一覧を移行台帳へ保存する。
4. 開発・検証端末のPDFが混在していないことを確認する。

PDF一覧の記録例:

```powershell
Get-ChildItem -LiteralPath .\patent_pool -Filter *.pdf -File |
  Get-FileHash -Algorithm SHA256 |
  Sort-Object Path |
  Export-Csv -NoTypeInformation -Encoding UTF8 .\patent-pool-manifest.csv
```

`patent-pool-manifest.csv` はPDF情報を含む運用台帳としてGitへ追加しない。

### 9.2 新しい本番リサーチを作る

1. PDFを先に `patent_pool/` へ配置する。
2. UIの「リサーチ管理」から、名前、ID、説明、自社技術、CSVを登録する。
3. CSV名を `patent_list_yyyymmddHHMMSS.csv` にする。
4. PDF一致、未発見、複数候補、警告を保存前に確認する。
5. 出願人グラフと企業ランキングを使う場合は、CSVの `出願人・権利者名` が空欄でないことを確認する。
6. 作成された `researches/<research_id>/` をGitへ追加しない。

CSVの正確な物理・列仕様は [PATENT_LIST_CSV_SPEC.md](PATENT_LIST_CSV_SPEC.md) を正とする。

## 10. Phase 5: 初回起動、SQLite構築、Codex接続

### 10.1 初回起動

エクスプローラーから `scripts/start.bat` を実行するか、PowerShellから次を実行する。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -IdleTimeoutMinutes 30
```

起動処理は次を行う。

1. Python 3.10以上と必要パッケージを確認する。
2. `runtime/` を作成する。
3. 最初に参照された環境のSQLiteを最新schemaで作成する。通常起動ではNORMAL、DEBUGへ切り替えた時点またはDEBUG検査時にDEBUG DBを作成する。
4. リサーチ入力とPDFプールを同期する。
5. アプリ専用Ollamaを空きlocalhostポートで起動する。
6. `127.0.0.1:8765` でPatentViewerを起動する。
7. EdgeまたはChromeをアプリモードで開く。
8. `runtime/server-control.json`、stdout/stderrログを生成する。

### 10.2 健全性確認

```powershell
$health = Invoke-RestMethod http://127.0.0.1:8765/api/health
$health | ConvertTo-Json -Depth 8
python tools/migrate_to_sqlite.py --environment normal --verify-only --json
```

完了条件:

- [ ] `health.ok = true`
- [ ] `health.environment = normal`
- [ ] `ollama_runtime.running = true`
- [ ] `ollama_runtime.error` が空
- [ ] SQLite `schema_version = 1`
- [ ] `integrity = ok`
- [ ] `missing_payloads = 0`
- [ ] `invalid_vectors = 0`
- [ ] `orphan_vectors = 0`
- [ ] 本番リサーチ作成前はNORMAL `researches = 0`, `documents = 0`
- [ ] 本番リサーチ作成後は、UI件数が承認済みCSVの行数と一致する
- [ ] PDF配置後、UIのPDF POOL件数と配置件数が一致する

PDFをSQLite初回同期後に追加した場合も、PDFプールの更新時刻が署名へ含まれるため次回同期対象になる。確実に再構築する場合は、分析ジョブとサーバーを停止したうえで次を使う。

```powershell
python tools/migrate_to_sqlite.py --environment normal --force
python tools/migrate_to_sqlite.py --environment normal --verify-only --json
```

### 10.3 Codex接続

clone先を今後変更しないことを確認してから実行する。

```powershell
python tools/patent_viewer.py mcp-config --write
python tools/patent_viewer.py doctor
```

生成物は `.codex/mcp.local.json` で、端末固有のPython・リポジトリ絶対パスを含む。Gitへ追加しない。Codexが設定をすでに読み込んでいる場合は、Codexまたは接続タスクを再起動してrepo-local MCPを読み直す。

`doctor` の完了条件:

- [ ] python OK
- [ ] repo OK
- [ ] mcp OK
- [ ] server OK
- [ ] protocol OK
- [ ] browser 1 client以上
- [ ] visible target 1件以上
- [ ] 最終表示が `READY`

## 11. Phase 6: 本番データ投入前の受入テスト

### 11.1 自動テスト

```powershell
python -m unittest discover -s tests -v
node --check public/assets/app.js
python tools/patent_viewer.py doctor
```

Node.jsを本番端末へ入れない運用では、`node --check` はrelease作成側で必須、移行先では任意とする。

画像回帰:

```powershell
python tools/ui_visual_check.py
```

EdgeまたはChromeが必要である。意図的なUI変更ではない限り `--update-baselines` を使わない。

### 11.2 人手によるUI確認

- [ ] NORMALで正しいリサーチ名と件数が表示される
- [ ] 脅威マップが5×5で表示される
- [ ] 技術マップが表示される
- [ ] 0件セルはクリックできない
- [ ] 1件以上のセルを選ぶと「マップ → セル分析 → 文献一覧／根拠」の順になる
- [ ] 年次推移と出願人構成を切り替えられる
- [ ] 年次・出願人グラフが権利化、審査中、公開の色で表示される
- [ ] 出願人構成は上位10社である
- [ ] サイドバー企業ランキングに全体割合の背景バーが表示される
- [ ] 0件条件ではセル分析グラフが表示されない
- [ ] ホワイトスペース診断カードは表示されない
- [ ] PDFを開ける
- [ ] 対象文献と根拠がセル選択に連動する
- [ ] 検索、年、法的状態、企業条件が反映される

## 12. Phase 7: 少数文献でパイプラインを実証する

大量分析の前に、DEBUG固定フィクスチャまたは承認済み本番PDFの少数件で確認する。

### 12.1 Preflight

UIの `LLM preflight` を実行する。これはモデル推論をせず、次を確認する。

- PDF選択
- プロンプト
- JSON Schema
- NORMAL/DEBUG出力分離
- 専用Ollama接続
- 必要モデル
- 請求項構造

`ready` になるまで全件分析を開始しない。

### 12.2 段階実証

推奨経路はUIの `夜間一括分析` である。UI経路は専用OllamaのURL、自動worker数、Embedding batch、shard sizeを正しく引き渡す。

1. `前処理だけ実行` でPDF抽出と構造化を確認する。
2. 1〜数件を実行し、要約と請求項根拠を人が確認する。
3. pause、resume、cancelのいずれかをテストする。
4. 再実行時に完了済みタスクが再利用されることを確認する。
5. `analysis_error.json`、`run_manifest.json`、UIの状態を確認する。

CLIを使う場合、既定の `tools/run_research_pipeline.py` は `http://127.0.0.1:11434` を参照する。PatentViewer管理下のOllamaはランダムポートなので、理由なくCLIを直接実行しない。UIまたはCodex collaborationのpipeline blockを使う。

## 13. Phase 8: 全件分析を開始する

開始前に受入記録へ次を記載する。

- release commit/tag
- research ID
- 対象文献数
- PDF一致、未発見、複数候補
- 使用モデル名とID
- `/api/health` のruntime config
- generation workers
- embedding batch size
- shard size
- durable shards
- cooldown秒
- overwrite有無
- 開始予定日時
- 停止、再開、cancelの担当者
- バックアップ先

Codex collaborationから実行する場合は、先に `get_pipeline_overview` と `plan_block` を使い、対象と負荷を確認する。全件実行は `confirmation: RUN_LOCAL_LLM` が必要である。

ブラウザを閉じてもサーバー上のジョブは継続する。実行中はアイドル終了が抑止される。エラーは文献単位で記録され、次文献へ進む。Ollama接続障害、全体クラスタリング障害、cancelは全体停止として扱う。

## 14. 本番稼働の完了条件

- [ ] 固定release revisionから構築した
- [ ] 作業ツリーがclean
- [ ] 環境台帳・移行台帳・受入記録がある
- [ ] Python依存とモデルIDを記録した
- [ ] 開発端末のSQLite、runtime、分析結果をコピーしていない
- [ ] 本番PDFの利用権限とSHA-256一覧を記録した
- [ ] SQLite integrity、schema、vector検証が成功した
- [ ] NORMALとDEBUGが分離している
- [ ] 全自動テストが成功した
- [ ] doctorがREADY
- [ ] UI受入項目が成功した
- [ ] 少数文献の抽出、生成、Embedding、保存、再開が成功した
- [ ] 人が要約と根拠を承認した
- [ ] 全件分析の設定と承認を記録した
- [ ] バックアップと復旧手順を確認した

## 15. 起動・停止・日常確認

起動:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

ブラウザなし:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -NoBrowser
```

停止:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

正常停止では、`runtime/server-control.json` のランダムトークンを使ってlocalhost shutdown APIを呼ぶ。タスクマネージャーから強制終了する前に通常停止を試す。

日常確認:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health | ConvertTo-Json -Depth 8
python tools/migrate_to_sqlite.py --environment normal --verify-only
git status --short
```

## 16. バックアップと復旧

本番開始後に生成されたデータは本番資産になる。少なくとも次を一組でバックアップする。

- `runtime/normal/patent_viewer.sqlite3` の整合性あるスナップショット
- `patent_pool/`
- `researches/<research>/` の入力と生成物
- `runtime/shared/extractions/`
- `runtime/collaboration/audit.jsonl`
- 必要な `config/organization_registry.json`
- release commit/tag
- Ollamaモデル名とID
- 環境台帳、移行台帳、受入記録

バックアップ前に分析ジョブを停止または完了させ、PatentViewerを正常停止する。停止後にSQLiteをコピーするか、SQLite backup APIを使う。稼働中のDBファイルだけをコピーしない。

復旧時は `runtime/server-control.json` と `.codex/mcp.local.json` を復元しない。MCP設定は復旧先の固定パスで再生成する。

## 17. よくある失敗と切り分け

| 症状 | 主な原因 | 確認・対処 |
|---|---|---|
| Pythonが見つからない | PATHまたは候補パスにPython 3.10以上がない | Python 3.12系を導入し、`python --version` を確認 |
| 起動直後に終了 | Python依存不足、ポート競合 | `runtime/server.stderr.log` を確認。`requirements.txt` を導入 |
| Ollamaが起動しない | 実行ファイル未検出、driver、モデルストア | `ollama --version`、`ollama list`、`ollama serve` を確認。必要なら `PATENT_VIEWER_OLLAMA_EXE` |
| Preflightでモデル不足 | 必須モデル未取得 | 3モデルを `ollama pull` し、IDを記録 |
| PDF POOLが0 | `patent_pool/` が空または配置先違い | repo直下の `patent_pool/` と拡張子を確認 |
| 文献はあるが分析できない | PDF未一致 | CSV番号、PDF名、UIの未発見・複数候補を確認 |
| 企業グラフが空 | CSVの出願人欄が空、または旧DB同期 | `出願人・権利者名` を確認し、必要なら `--force` 同期 |
| グラフの棒や色が古い | ブラウザキャッシュまたはサーバー再起動前 | ページ再読込。Python/API変更後はPatentViewerを再起動 |
| doctorのbrowser/targetsがNG | UI未起動、Codex接続前 | ブラウザでUIを開き、ロード完了後に再実行 |
| SQLite verifyがNG | 中断書込み、古いschema、不正vector | サーバーとジョブを止め、バックアップ後に検証。推測でDBを編集しない |
| GPU OOM | worker/batch過大、他プロセスがVRAM使用 | `/api/health` と `nvidia-smi` を確認。他負荷を止め、少数件で再検証 |
| 大量処理でRAM不足 | durable shards条件未達 | 24 GB級GPU・64 GB RAM環境を検討し、少数件の生成量から容量計画 |
| MCPが旧cloneを指す | clone後に移動、古いlocal config | 固定パスで `mcp-config --write` を再実行しCodexを再接続 |

## 18. 環境台帳テンプレート

初回構築時に次を複製して記入する。

```text
構築日時:
担当者/Codex task:
用途: production / staging
リポジトリURL:
release tag:
commit SHA:
git status clean: yes / no
clone絶対パス:

OS:
PowerShell:
CPU:
RAM:
GPU:
GPU driver:
VRAM total/free:
ローカルディスク空き:
Python:
pip:
pypdf:
numpy:
Pillow:
playwright:
Edge/Chrome:

Ollama:
gemma4:e4b ID:
gpt-oss:20b ID:
qwen3-embedding:8b ID:

PatentViewer version:
Collaboration protocol:
SQLite schema:
server URL:
managed Ollama URL:
generation workers:
embedding batch size:
shard size:
durable shards:

research ID:
CSV filename/SHA-256:
CSV rows:
PDF files:
PDF manifest SHA-256:
PDF matched:
PDF missing:
multiple candidates:

unit/API tests:
JavaScript syntax:
SQLite integrity:
doctor:
UI acceptance:
LLM preflight:
pilot prepare:
pilot execute:
human quality approval:

full run approval:
full run started:
full run completed:
run manifest:
backup location:
known deviations:
```

この台帳が完成し、Phase 14の完了条件を満たした時点を本番稼働開始とする。
