# 本番端末の新規構築手順

> **現行の正本:** 新しい端末・Codex環境の具体的な構築、空のNORMAL環境、
> 環境台帳、受入条件、障害対応は
> [`ENVIRONMENT_MIGRATION_RUNBOOK.md`](ENVIRONMENT_MIGRATION_RUNBOOK.md)
> を使用する。この文書はデータ境界の基本方針を補足する。

## 目的と前提

この文書は、別端末のCodexがPatentViewerの本番環境を新規構築するときの作業指示である。

この文書は、NORMALリサーチ入力を含まないreleaseを使い、完全な空状態から始める場合の補足資料である。具体的な手順は
[`ENVIRONMENT_MIGRATION_RUNBOOK.md`](ENVIRONMENT_MIGRATION_RUNBOOK.md)
を正とする。

空環境用releaseを使う本番端末は、開発端末からデータを移行せず、空の状態から運用を開始する。

- 開発端末のPDFを移行しない。
- 開発端末のSQLite DB、JSON分析結果、抽出キャッシュ、実行履歴を移行しない。
- 本番端末にだけ存在する新しいPDFを登録する。
- 分析結果は本番端末でゼロから蓄積する。
- Gitで移すのは、アプリ本体、スキーマ、プロンプト、テスト、セットアップ手順だけとする。

## 移行先のCodexへの最初の指示

移行先では、Codexに次のように依頼する。

> このリポジトリの `docs/ENVIRONMENT_MIGRATION_RUNBOOK.md` を最後まで読み、Phase 0から順番に空のNORMAL環境を構築してください。`docs/PRODUCTION_BOOTSTRAP.md` のデータ境界も確認してください。実行前に現在の実装と文書の差分を確認し、未実装のコマンドを存在するものとして扱わないでください。

Codexは、作業開始時にこの文書だけでなく、`README.md`、`docs/CODEX_COLLABORATION.md`、`.gitignore`、`requirements.txt`、実際の起動・DB初期化コードも確認すること。

## データ境界

### 開発端末から持ち込むもの

- Gitリポジトリの追跡対象ファイル
- 必要に応じて、確定したrelease tagまたはcommit ID

### 開発端末から持ち込まないもの

- `patent_pool/` 内のPDF
- SQLite DBファイル
- `runtime/`
- 通常リサーチの `results/`、`runs/`、`pipeline/`、`clustering/`
- 抽出本文、Embedding、LLM要求・応答、監査ログ
- `.codex/mcp.local.json`
- `runtime/server-control.json`
- `researches/` のNORMALリサーチ入力、自社技術定義、CSV、生成物
- 固定fixture以外の `debug_data/researches/`
- `config/organization_registry.json` とリサーチ別企業グループ設定

`.codex/mcp.local.json` と `runtime/server-control.json` は端末固有情報を含むため、本番端末で再生成する。

### 本番端末で新しく作るもの

- 空のSQLite DB
- 本番PDFプール
- 本番リサーチ
- 本番PDFから生成した抽出物、Embedding、分析結果、監査履歴

## 現在の実装に関する注意

2026年7月25日時点でSQLite保存、スキーマ管理、サーバー側検索・ページング、
Embeddingのfloat32 BLOB保存は実装済みである。詳細は
[`docs/SQLITE_STORAGE.md`](SQLITE_STORAGE.md) を参照する。

移行先のCodexは最初に現在のコードを確認し、次を検証すること。

1. SQLiteのスキーマ管理と初期化処理が実装済みか。
2. 初回起動時に空DBが安全に作成されるか。
3. `schema_migrations` 相当のバージョン管理があるか。
4. CSV取込、検索、分析状態、結果保存がSQLiteを使用しているか。
5. APIが5万件を一括返却せず、ページングとサーバー側検索を使用しているか。
6. Embeddingが巨大なJSON配列ではなく、float32 BLOBまたは同等のバイナリ形式で保存されるか。

実装と文書に差がある場合は本番データ投入前に解消する。存在しないDB初期化コマンドを
推測して実行したり、手作業で暫定テーブルを作ったりしない。

## 本番構築フロー

### 1. コードを取得する

確定した本番用commitまたはrelease tagをcloneする。作業ツリーが意図したrevisionであることを確認する。

### 2. 実行環境を確認する

最低限、次を確認する。

- Python 3.10以上
- `requirements.txt` の依存パッケージ
- Git
- 対応ブラウザ
- Ollama
- 分析に必要な生成モデルとEmbeddingモデル
- 十分なディスク容量、RAM、VRAM

現行モデル名はコードと設定を正として確認する。2026年7月25日時点の想定は次のとおり。

- 生成：`gemma4:e4b`
- Embedding：`qwen3-embedding:8b`

モデル名だけでなく、可能ならOllamaのモデル情報またはdigestも構築記録へ残す。

### 3. Python依存関係を導入する

```powershell
python -m pip install -r requirements.txt
```

本番再現性用のlockファイルが追加されている場合は、`requirements.txt` よりlockファイルを優先する。

### 4. 空のデータストアを初期化する

通常起動で最新スキーマの空DBが自動作成される。起動後、次のコマンドで検証する。

```powershell
python tools/migrate_to_sqlite.py --environment normal --verify-only
```

releaseで期待する初期状態は次のとおり。

- 本番用SQLite DBが新規作成される。
- スキーマが最新バージョンになる。
- PDF件数、リサーチ件数、分析結果件数がすべて0件である。
- DEBUG用固定フィクスチャと本番データが分離される。
- DBおよび本番生成物がGit追跡対象外である。
- DBに保存するファイルパスはリポジトリまたはデータルートからの相対パスである。

既存DBの復元や開発端末DBのコピーは行わない。

### 5. PatentViewerとCodex連携を設定する

```powershell
python tools/patent_viewer.py start
python tools/patent_viewer.py mcp-config --write
python tools/patent_viewer.py doctor
```

`mcp-config --write` は本番端末固有の絶対パスを `.codex/mcp.local.json` へ生成する。このファイルはGitへ追加しない。

### 6. テストと診断を実行する

```powershell
python -m unittest discover -v
python tools/patent_viewer.py doctor
```

必要に応じて、UIのvisual checkと少数のDEBUG smoke testも実行する。本番PDFを大量投入する前に、空DBで起動・停止・再起動・マイグレーションが正常であることを確認する。

### 7. 本番PDFを登録する

本番端末にある新しいPDFだけを所定のPDFプールへ配置または取込する。

- 開発端末のPDFを混在させない。
- 同一PDFの判定には、ファイル名だけでなくSHA-256を使用する。
- 大量PDFを単一ディレクトリへ置かない設計が実装されている場合は、そのオブジェクト配置規則に従う。
- DBへPDF本体を格納せず、DBには識別情報、相対パス、SHA-256、サイズ、状態を保存する。

### 8. 本番リサーチを新規作成する

本番用CSVと自社技術定義を使って、新しいリサーチを作成する。PDF一致件数、未発見件数、重複件数、警告を確認してから保存する。

### 9. 少数件で分析を検証する

全件分析の前に少数PDFで次を確認する。

- PDF抽出
- 請求項構造
- 生成モデル接続
- Embeddingモデル接続
- 結果保存
- 再開処理
- UI検索とページング
- Codexからの文献検索

モデル出力を人が確認してから対象件数を増やす。

### 10. 本番分析を開始する

5万件規模では、一度に全件をブラウザへ読み込まない。サーバー側検索、ページング、集計API、差分分析、永続チェックポイントを使用する。

分析実行前に、対象件数、予想LLM呼出し数、必要ディスク容量、推定時間、停止・再開方法を記録する。

## 本番構築の完了条件

- 開発端末由来のPDF、DB、分析結果が存在しない。
- 本番用SQLite DBが空の状態から作成された記録がある。
- DBスキーマバージョンが最新である。
- 本番PDFだけが登録されている。
- NORMALとDEBUGの保存先が分離されている。
- 全unit testとdoctorが成功する。
- 少数PDFの分析と再開が成功する。
- APIとUIが全件一括ロードを行わない。
- DB、PDF、生成物、端末固有設定がGitへ追加されていない。
- 使用したGit commit、Python、依存関係、Ollama、モデル情報が記録されている。

## 本番開始後のバックアップ

初回構築では旧端末データを移行しないが、本番開始後に生成されたデータは本番資産になる。少なくとも次をバックアップ対象とする。

- SQLite DBの整合性あるスナップショット
- 本番PDF
- DBが参照する大容量成果物
- 必要な監査ログ
- Git commit、DBスキーマバージョン、モデル情報を含むmanifest

稼働中のSQLiteファイルをそのままコピーせず、SQLite backup API、または安全な停止とWAL checkpointを使う。
