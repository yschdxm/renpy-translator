"""剧情图/关系图仓库：story_nodes、story_edges、char_relations、char_avatars

story_* 是纯派生数据：每次构建先清后插（全量重算），游戏版本更新时
由更新管线调 clear_story_graph 失效。
char_relations 含人工编辑（source=manual），AI 重算时只清 source=ai 的行。
char_avatars 同样是派生数据，随剧情图构建重算。
"""

import sqlite3

from .base import _auto_reconnect


class GraphRepo:
    """剧情图与人物关系图表"""

    # ========== 剧情图 ==========

    @_auto_reconnect
    def replace_story_graph(self, nodes: list[dict], edges: list[dict]):
        """全量重建剧情图（事务内先清后插）"""
        with self._transaction():
            self._conn.execute("DELETE FROM story_nodes")
            self._conn.execute("DELETE FROM story_edges")
            self._conn.executemany(
                """INSERT INTO story_nodes
                   (label, file_path, line_start, line_end, speakers_json,
                    dialogue_count, first_text, first_text_cn, is_entry,
                    has_return, is_terminal, thumb_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(n['label'], n['file'], n['line_start'], n['line_end'],
                  n['speakers_json'], n['dialogue_count'], n['first_text'],
                  n.get('first_text_cn', ''),
                  int(n['is_entry']), int(n['has_return']),
                  int(n['is_terminal']), n.get('thumb_file', ''))
                 for n in nodes])
            self._conn.executemany(
                """INSERT INTO story_edges
                   (source, target, kind, branch, text, text_cn, expr, line)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(e['source'], e['target'], e['kind'], e['branch'],
                  e['text'], e.get('text_cn', ''), e['expr'], e['line'])
                 for e in edges])
            self._conn.execute(
                "INSERT OR REPLACE INTO project_meta (key, value) "
                "VALUES ('graph_built_at', datetime('now', 'localtime'))")

    @_auto_reconnect
    def get_story_graph(self) -> dict:
        nodes = [dict(r) for r in self._conn.execute(
            "SELECT * FROM story_nodes ORDER BY file_path, line_start").fetchall()]
        edges = [dict(r) for r in self._conn.execute(
            "SELECT * FROM story_edges ORDER BY id").fetchall()]
        return {'nodes': nodes, 'edges': edges}

    @_auto_reconnect
    def clear_story_graph(self):
        """版本更新/重建前失效（含场景图与 meta 时间戳）"""
        with self._transaction():
            self._conn.execute("DELETE FROM story_nodes")
            self._conn.execute("DELETE FROM story_edges")
            self._conn.execute("DELETE FROM story_scenes")
            self._conn.execute("DELETE FROM story_scene_edges")
            self._conn.execute(
                "DELETE FROM project_meta WHERE key='graph_built_at'")

    # ========== 剧情场景 ==========

    @_auto_reconnect
    def replace_story_scenes(self, scenes: list[dict], edges: list[dict]):
        """全量重建场景图（与 label 级同事务，保持一致）"""
        with self._transaction():
            self._conn.execute("DELETE FROM story_scenes")
            self._conn.execute("DELETE FROM story_scene_edges")
            self._conn.executemany(
                """INSERT INTO story_scenes
                   (scene_id, title, summary, labels_json, speakers_json,
                    first_text, first_text_cn, dialogue_count,
                    translated_count, thumb_file, is_entry, is_ending,
                    is_return, file_path, line_start)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(s['scene_id'], s.get('title', ''), s.get('summary', ''),
                  s['labels_json'], s['speakers_json'], s['first_text'],
                  s['first_text_cn'], s['dialogue_count'],
                  s.get('translated_count', 0), s['thumb_file'],
                  int(s['is_entry']), int(s['is_ending']),
                  int(s['is_return']), s['file_path'], s['line_start'])
                 for s in scenes])
            self._conn.executemany(
                """INSERT INTO story_scene_edges
                   (source, target, texts_json, texts_cn_json, branch,
                    has_call, unresolved)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(e['source'], e['target'], e['texts_json'],
                  e['texts_cn_json'], e['branch'], int(e['has_call']),
                  int(e['unresolved'])) for e in edges])

    @_auto_reconnect
    def get_story_scenes(self) -> dict:
        scenes = [dict(r) for r in self._conn.execute(
            "SELECT * FROM story_scenes ORDER BY line_start").fetchall()]
        edges = [dict(r) for r in self._conn.execute(
            "SELECT * FROM story_scene_edges ORDER BY id").fetchall()]
        return {'scenes': scenes, 'edges': edges}

    @_auto_reconnect
    def get_scene_dialogue(self, labels: list[str]) -> list[dict]:
        """场景包含 label 的全部台词（原文+译文，按文件/行号排序）"""
        if not labels:
            return []
        marks = ','.join('?' * len(labels))
        rows = self._conn.execute(
            f"""SELECT file_path, line_number, label, character,
                       original_text, translated_text, is_translated
                FROM dialogues WHERE label IN ({marks})
                ORDER BY file_path, line_number""", labels).fetchall()
        return [dict(r) for r in rows]

    @_auto_reconnect
    def get_label_translation_stats(self) -> dict:
        """各 label 的台词数与已译数（场景翻译进度聚合用）"""
        rows = self._conn.execute(
            """SELECT label, COUNT(*) AS total,
                      SUM(is_translated) AS translated
               FROM dialogues GROUP BY label""").fetchall()
        return {r['label']: {'total': r['total'],
                             'translated': r['translated'] or 0}
                for r in rows}

    @_auto_reconnect
    def story_graph_stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM story_nodes").fetchone()
        edge_row = self._conn.execute(
            "SELECT COUNT(*) AS n, SUM(target IS NULL) AS unres "
            "FROM story_edges").fetchone()
        return {'nodes': row['n'], 'edges': edge_row['n'] or 0,
                'unresolved': edge_row['unres'] or 0,
                'built_at': self.get_meta('graph_built_at')}

    # ========== 角色立绘 ==========

    @_auto_reconnect
    def replace_char_avatars(self, avatars: dict[str, str]):
        """全量重建角色立绘映射 {变量名: 相对 source_root 的 posix 路径}"""
        with self._transaction():
            self._conn.execute("DELETE FROM char_avatars")
            self._conn.executemany(
                "INSERT INTO char_avatars (variable, image_path, source) "
                "VALUES (?, ?, 'auto')",
                [(v, p) for v, p in avatars.items() if p])

    @_auto_reconnect
    def get_char_avatars(self) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT variable, image_path FROM char_avatars").fetchall()
        return {r['variable']: r['image_path'] for r in rows}

    # ========== 人物关系 ==========

    @_auto_reconnect
    def replace_ai_relations(self, relations: list[dict]):
        """重建 AI 推断的关系（保留 source=manual 的人工编辑）"""
        with self._transaction():
            self._conn.execute(
                "DELETE FROM char_relations WHERE source != 'manual'")
            self._conn.executemany(
                """INSERT INTO char_relations
                   (source_var, target_var, relation, category, polarity,
                    description, cooccurrence, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                [(r['source_var'], r['target_var'], r['relation'],
                  r.get('category', 'other'), r.get('polarity', ''),
                  r.get('description', ''), r.get('cooccurrence', 0),
                  r.get('source', 'ai')) for r in relations])

    @_auto_reconnect
    def get_relations(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM char_relations ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    @_auto_reconnect
    def upsert_relation(self, rel: dict) -> int:
        """人工新增/更新关系边（id 存在则更新），返回 id"""
        if rel.get('id'):
            self._conn.execute(
                """UPDATE char_relations
                   SET source_var=?, target_var=?, relation=?, category=?,
                       polarity=?, description=?, source='manual',
                       updated_at=datetime('now', 'localtime')
                   WHERE id=?""",
                (rel['source_var'], rel['target_var'], rel['relation'],
                 rel.get('category', 'other'), rel.get('polarity', ''),
                 rel.get('description', ''), rel['id']))
            self._conn.commit()
            return rel['id']
        cur = self._conn.execute(
            """INSERT INTO char_relations
               (source_var, target_var, relation, category, polarity,
                description, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'manual',
                       datetime('now', 'localtime'))""",
            (rel['source_var'], rel['target_var'], rel['relation'],
             rel.get('category', 'other'), rel.get('polarity', ''),
             rel.get('description', '')))
        self._conn.commit()
        return cur.lastrowid

    @_auto_reconnect
    def delete_relation(self, rel_id: int):
        self._conn.execute("DELETE FROM char_relations WHERE id=?", (rel_id,))
        self._conn.commit()

    @_auto_reconnect
    def set_character_factions(self, factions: dict[str, str]):
        """按角色身份键写入阵营（AI 关系生成附带产出）"""
        with self._transaction():
            for key, faction in factions.items():
                self._conn.execute(
                    "UPDATE characters SET faction=? "
                    "WHERE variable=? OR (variable='' AND display_name=?)",
                    (faction, key, key))
