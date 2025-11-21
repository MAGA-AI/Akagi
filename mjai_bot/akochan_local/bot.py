import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any

class Bot:
    name = "akochan_local"

    def __init__(self, settings=None):
        # settings なし起動にも対応（Controller は Bot() で呼ぶ）
        self._settings = dict(settings or {})
        from pathlib import Path
        self.workdir = Path(self._settings.get("akochan_dir", str(Path.home() / "src" / "akochan")))
        self.exe = self.workdir / "system.exe"
        self.seat = int(self._settings.get("seat", 0))  # 0..3
        self._events = []

    def reset(self) -> None:
        self._events.clear()

    def close(self) -> None:
        pass

    def on_events(self, events: List[Dict[str, Any]]) -> None:
        # すべて保持（重複なく最新状態へ）
        self._events = list(events)

    def need_action(self) -> Optional[Dict[str, Any]]:
        """
        直近の状態で 1 回だけ akochan を呼び、アクション JSON を得る。
        akochan は mjai_log <file> <id> でファイルを読む実装なので、
        tempfile に書き出してから起動する。
        """
        if not self.exe.exists():
            raise FileNotFoundError(f"akochan executable not found: {self.exe}")

        # いまアクションが必要な局面でない場合は None を返して上位に委ねる。
        # （単純化のため常に試す→失敗時 None 返却でもOK）
        if not self._events:
            return None

        # 一時ファイルに mjai イベント列を書き出す
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            for ev in self._events:
                tf.write(json.dumps(ev, ensure_ascii=False) + "\n")
            temp_path = Path(tf.name)

        try:
            # akochan 実行（標準出力の最後の JSON 行を採用）
            cmd = [str(self.exe), "mjai_log", str(temp_path), str(self.seat)]
            proc = subprocess.run(
                cmd, cwd=str(self.workdir),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            if proc.returncode != 0:
                # 必要なら proc.stderr をログに出す
                return None

            out = proc.stdout.strip().splitlines()
            # 出力中の“最後の JSON っぽい行”を拾う（先頭や途中にログが混ざる場合あり）
            for line in reversed(out):
                line = line.strip()
                if not line:
                    continue
                try:
                    action = json.loads(line)
                    # 最低限の形式チェック
                    if isinstance(action, dict) and "type" in action:
                        return action
                except Exception:
                    continue
            return None
        finally:
            # 一時ファイルを掃除
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
