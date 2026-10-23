# あさラジ

毎朝5:30（日本時間）にニュースを集め、2人の掛け合い10分のポッドキャストを自動で作る。

- 流れ: RSS収集 → Geminiが検索で裏取り → 台本 → Gemini TTS（2人の声）→ mp3 → feed.xml
- 動かす場所: GitHub Actions（`.github/workflows/daily.yml`）。PCの電源は不要
- 聞き方: ポッドキャストアプリに `https://hamburg1106-collab.github.io/asa-radio/feed.xml` を「URLで追加」
- 過去の回は `episodes` ブランチに直近14回だけ残る（毎回1コミットで上書き）

## 設定

- APIキー: リポジトリの Secrets に `GEMINI_API_KEY`
- 声・話者: `make_episode.py` の `HOSTS`
- ニュース元: `FEEDS`
- モデル: 環境変数 `TEXT_MODEL` / `TTS_MODELS`（カンマ区切り。429なら次を使う）

## 手で動かす

- GitHubの Actions タブ → daily-episode → Run workflow
- 手元での試運転（APIなし）: `python make_episode.py --out site --dry-run`
