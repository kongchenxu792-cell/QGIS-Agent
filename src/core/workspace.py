"""workspace.py — 工作区 manifest 管理（P3-1）。

把散落的 QGIS 图层状态打包成可命名 / 可保存 / 可恢复 / 可切换的工作区。
本片只实现 manifest 与四个 action（workspace_new / workspace_save /
workspace_open / workspace_list），UI 面板与切换 / 回滚留待 P3-2/P3-3。

工作区根目录：user_data/workspaces/<id>/workspace.json

workspace.json 结构：
    {
        "id": "<时间戳短码>",
        "name": "神户案例",
        "country": "jp",
        "created_at": "2026-08-17T13:31:59",
        "updated_at": "2026-08-17T13:31:59",
        "notes": "",
        "layers": [{"name": "...", "source": "...", "crs": "...", "style": {}}]
    }
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from qgis.core import QgsProject

_log_name = "instruction_mapper"


class WorkspaceManager:
    """工作区管理器：创建 / 保存 / 打开 / 列出 / 删除工作区 manifest。

    Parameters
    ----------
    root_dir : str, optional
        工作区根目录。默认 user_data/workspaces（与 user_data/ 一致），
        测试可注入临时目录避免污染真实数据。
    """

    def __init__(self, root_dir: Optional[str] = None) -> None:
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.root_dir = root_dir or os.path.join(project_root, "user_data", "workspaces")

    # ── 内部路径工具 ─────────────────────────────────────

    def _ws_dir(self, workspace_id: str) -> str:
        return os.path.join(self.root_dir, workspace_id)

    def _manifest_path(self, workspace_id: str) -> str:
        return os.path.join(self._ws_dir(workspace_id), "workspace.json")

    @staticmethod
    def _new_id() -> str:
        """生成工作区 id：时间戳短码（秒级 + 2 位随机后缀防同秒冲突）。"""
        import random
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{ts}{random.randint(10, 99)}"

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _read_manifest(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        """读取 manifest（不存在返回 None，损坏抛异常由调用方处理）。"""
        path = self._manifest_path(workspace_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ── 公开 API ─────────────────────────────────────────

    def create(self, name: str, country: str = "jp", notes: str = "") -> Dict[str, Any]:
        """创建新工作区：建目录 + 写入空 manifest。

        Parameters
        ----------
        name : str
            工作区名称（如「神户案例」）。
        country : str, optional
            国别代码（默认 jp），国别配置切换在后续切片生效。
        notes : str, optional
            备注。

        Returns
        -------
        dict
            {"success": bool, "message": str, "workspace_id": str, "manifest": dict}
        """
        ws_id = self._new_id()
        ws_dir = self._ws_dir(ws_id)
        try:
            os.makedirs(ws_dir)
        except FileExistsError:
            # 同秒随机后缀冲突概率极低，重试一次
            ws_id = self._new_id()
            ws_dir = self._ws_dir(ws_id)
            os.makedirs(ws_dir)

        now = self._now()
        manifest: Dict[str, Any] = {
            "id": ws_id,
            "name": name or ws_id,
            "country": country,
            "created_at": now,
            "updated_at": now,
            "notes": notes or "",
            "layers": [],
        }
        with open(self._manifest_path(ws_id), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return {
            "success": True,
            "message": f"工作区已创建：{manifest['name']}（{ws_id}）",
            "workspace_id": ws_id,
            "manifest": manifest,
        }

    def save(self, workspace_id: str, project: Any = None) -> Dict[str, Any]:
        """扫描 QgsProject 当前图层，序列化 name/source/crs/style 到 manifest。

        更新 updated_at；layers 顺序按 mapLayers() 当前顺序。
        """
        manifest = self._read_manifest(workspace_id)
        if manifest is None:
            return {"success": False, "message": f"工作区不存在：{workspace_id}"}

        proj = project or QgsProject.instance()
        layers: List[Dict[str, Any]] = []
        for layer in proj.mapLayers().values():
            crs_authid = ""
            try:
                crs = layer.crs()
                if crs is not None and crs.isValid():
                    crs_authid = crs.authid()
            except Exception:
                crs_authid = ""
            layers.append({
                "name": layer.name(),
                "source": layer.source(),
                "crs": crs_authid,
                "style": {},
            })

        manifest["layers"] = layers
        manifest["updated_at"] = self._now()
        with open(self._manifest_path(workspace_id), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return {
            "success": True,
            "message": f"工作区已保存：{manifest['name']}（{len(layers)} 个图层）",
            "workspace_id": workspace_id,
            "layer_count": len(layers),
            "manifest": manifest,
        }

    def open(self, workspace_id: str, project: Any = None) -> Dict[str, Any]:
        """读 manifest → 按 source 路径加载图层回 QgsProject → 返回摘要。

        - 复用 core.layer_loader.create_layer_from_path 加载图层。
        - 恢复顺序按 manifest 中 layers 顺序。
        - 源文件缺失 / 加载失败 → 显式报错（success=False），不静默跳过。
        - country 字段暂存返回，国别配置切换在后续切片生效。
        """
        manifest = self._read_manifest(workspace_id)
        if manifest is None:
            return {"success": False, "message": f"工作区不存在：{workspace_id}"}

        from core.layer_loader import create_layer_from_path

        proj = project or QgsProject.instance()
        loaded: List[str] = []
        missing: List[Dict[str, Any]] = []

        for entry in manifest.get("layers", []):
            src = entry.get("source", "") or ""
            name = entry.get("name", "")
            if not src or not os.path.exists(src):
                missing.append({"name": name, "source": src})
                continue
            try:
                layer = create_layer_from_path(src)
                if name and layer.name() != name:
                    layer.setName(name)
                proj.addMapLayer(layer)
                loaded.append(name)
            except Exception as exc:  # noqa: BLE001 - 逐图层隔离，失败不中断
                missing.append({"name": name, "source": src, "error": str(exc)})

        base = {
            "workspace_id": workspace_id,
            "name": manifest.get("name", workspace_id),
            "country": manifest.get("country", ""),
            "loaded": loaded,
            "missing": missing,
        }
        if missing:
            missing_names = "、".join(
                (m.get("name") or m.get("source") or "未知图层") for m in missing
            )
            return {
                "success": False,
                "message": (
                    f"工作区打开失败：以下图层源文件缺失或无法加载：{missing_names}"
                    f"（已加载 {len(loaded)} 个图层）"
                ),
                **base,
            }
        return {
            "success": True,
            "message": f"工作区已打开：{manifest.get('name', workspace_id)}（{len(loaded)} 个图层）",
            **base,
        }

    def list(self) -> Dict[str, Any]:
        """扫描 workspaces/ 返回 [{id, name, country, updated_at}]，按更新时间倒序。"""
        if not os.path.isdir(self.root_dir):
            return {"success": True, "message": "暂无工作区", "workspaces": []}

        workspaces: List[Dict[str, str]] = []
        for ws_id in sorted(os.listdir(self.root_dir)):
            path = self._manifest_path(ws_id)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    m = json.load(f)
                workspaces.append({
                    "id": m.get("id", ws_id),
                    "name": m.get("name", ws_id),
                    "country": m.get("country", ""),
                    "updated_at": m.get("updated_at", ""),
                })
            except Exception:
                continue

        workspaces.sort(key=lambda w: w.get("updated_at", ""), reverse=True)
        return {
            "success": True,
            "message": f"共 {len(workspaces)} 个工作区",
            "workspaces": workspaces,
        }

    def delete(self, workspace_id: str) -> Dict[str, Any]:
        """删除工作区目录（附赠能力，物理删除需上层确认）。"""
        ws_dir = self._ws_dir(workspace_id)
        if not os.path.isdir(ws_dir):
            return {"success": False, "message": f"工作区不存在：{workspace_id}"}
        shutil.rmtree(ws_dir)
        return {"success": True, "message": f"工作区已删除：{workspace_id}"}


def get_workspace_manager(root_dir: Optional[str] = None) -> WorkspaceManager:
    """获取 WorkspaceManager 实例（无状态，直接新建即可）。"""
    return WorkspaceManager(root_dir=root_dir)
