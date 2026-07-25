# SQLite保存方式と移行手順

## 結論

PatentViewerの検索・一覧・集計・分析状態・Embeddingの正本はSQLiteで管理する。
PDF本体、抽出本文、LLMの監査用要求・応答はファイルのまま保持する。
これにより、画面の操作感を変えずに、5万件規模でも全JSON走査と全件メモリ展開を避ける。

DBはPython標準ライブラリの `sqlite3` を使用する。Codexに内包されたDBサービスではないため、
別端末でもPython 3.10以上とこのリポジトリがあれば同じスキーマを自動作成できる。

## 保存先

- NORMAL: `runtime/normal/patent_viewer.sqlite3`
- DEBUG: `runtime/debug/patent_viewer.sqlite3`
- PDF: 従来どおり `patent_pool/` 等のファイル領域

DB、PDF、`runtime/`、通常リサーチの生成物はGitへ追加しない。

## データの役割

SQLiteには、リサーチ、文献、企業、分析結果、集計用の列、解釈メモ、処理状態を保存する。
Embeddingは各ベクトルをfloat32 BLOBとして保存する。新規分析では巨大な
`embeddings.json` を作成せず、監査用にハッシュ、次元数、保存方式だけを
`embedding_manifest.json` に残す。

PDFと大容量の抽出本文はDBへ格納しない。DBには識別子、状態、相対参照だけを持たせる。
段階分析JSONは当面、監査・旧データ互換のため併存する場合があるが、画面と検索の読み取り経路はSQLiteを使用する。

## 既存環境を移行する

移行前にサーバーと分析ジョブを停止し、対象環境をバックアップする。

```powershell
python tools/migrate_to_sqlite.py --environment normal --force
python tools/migrate_to_sqlite.py --environment normal --artifacts
python tools/migrate_to_sqlite.py --environment normal --verify-only
```

- `--force`: CSV/旧JSONおよび結果JSONから文献行を再構築する。
- `--artifacts`: 旧Embeddingと段階成果物も取り込む。ファイル数が多いため時間がかかる。
- `--verify-only`: SQLite整合性、スキーマ、BLOB長、孤立Embeddingを検査する。
- NORMALとDEBUGの両方なら `--environment all` を使う。
- 機械可読の結果が必要なら `--json` を付ける。

移行元のJSONやPDFはコマンドによって削除されない。検証完了までは保持し、
不要な旧成果物を整理する場合は別作業としてバックアップ方針を決めてから行う。

## 新しい本番端末を空で構築する

旧端末のDB、PDF、分析結果をコピーしない。clone後に通常の起動を行うと、
最新スキーマの空DBが自動作成される。

```powershell
python -m pip install -r requirements.txt
python tools/patent_viewer.py start
python tools/patent_viewer.py mcp-config --write
python tools/patent_viewer.py doctor
python tools/migrate_to_sqlite.py --environment normal --verify-only
```

その後、新端末にだけ存在するPDFと新しいCSVからリサーチを作り、分析をゼロから蓄積する。
PDFはSQLiteへ格納しない。

## 5万件向けの実行経路

- Dashboardは文献配列を一括返却せず、集計値のみを先に返す。
- 文献一覧は `/api/researches/{id}/documents` の `limit` / `offset` で取得する。
- 検索、年、法的状態、マップセル、企業の絞込みはSQL側で行う。
- UIは初回200件を読み、スクロール時に続きを取得する。
- 大規模分析は永続シャードを有効にし、全EmbeddingをPythonのJSON配列として保持しない。
- クラスタリングはSQLiteのBLOBを順次読み、射影後の有界なベクトルで行う。

## 運用上の注意

- 同じDBへ複数の分析プロセスから同時に書き込む運用は避ける。
- SQLiteはWALモードを使用するが、ネットワーク共有ドライブ上のDB運用は避ける。
- 稼働中のDBを単純コピーせず、SQLite backup APIまたは安全な停止後のコピーを使う。
- DB破損時にPDFから分析を再現できるとは限らない。DB、PDF、監査成果物、使用commitとモデル情報を一組でバックアップする。
