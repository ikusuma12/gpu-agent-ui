import os
import time
import urllib.parse
import gradio as gr
import requests

# ミニPCの管理APIベースURL
API_BASE = os.getenv("API_BASE", "http://192.168.0.240:9000")
# ローカルスマートホームAPIのベースURL（同じ9000番なら変更不要、別ポートなら指定）
SMARTHOME_API_BASE = os.getenv("SMARTHOME_API_BASE", "http://192.168.0.240:8000")

# 電気代・為替レート設定（プラグ実測値からの損益分岐点計算用）
ELECTRICITY_COST_JPY = 31.0  # 1kWhあたりの電気代 (円)
USD_JPY_RATE = 155.0         # ドル円為替レート (円/$)

FLAG_CHOICES = ["free", "ritu_sleep", "ritu_use", "salad", "sd", "pending"]

GPU_METRIC_KEY = {
    "torrent": {"4070tis": "target", "5060ti": "gpu_1"},
    "terra":   {"4060ti": "target"},
    "north":   {"5080": "target"},
}

NODES = [
    {"id": "torrent", "name": "Torrent (Dual GPUサブ機)", "gpus": ["4070tis", "5060ti"]},
    {"id": "terra",   "name": "Terra (常時稼働機)",       "gpus": ["4060ti"]},
    {"id": "north",   "name": "North (メイン機)",         "gpus": ["5080"]},
]

# SwitchBot プラグミニ (JP) のデバイスマッピング
PLUG_DEVICES = {
    "north":   {"id": "A0764E204AEE", "type": "plug"},
    "torrent": {"id": "AC276E411EB2", "type": "plug"},
    "terra":   {"id": "D83BDA1667F6", "type": "plug"},
}

# SwitchBot API レート制限対策用のメモリキャッシュ（120秒保持）
_PLUG_CACHE = {"ts": 0.0, "data": {}}
PLUG_CACHE_TTL = 120.0


def fetch_all_plug_metrics() -> dict[str, dict]:
    """全ノードのプラグ実測値(weight_w, voltage_v)を取得（TTL内はキャッシュを返す）"""
    now = time.time()
    if now - _PLUG_CACHE["ts"] < PLUG_CACHE_TTL and _PLUG_CACHE["data"]:
        return _PLUG_CACHE["data"]

    result = {}
    for mid, cfg in PLUG_DEVICES.items():
        dev_id = cfg["id"]
        dev_type = urllib.parse.quote(cfg["type"], safe="")
        url = f"{SMARTHOME_API_BASE}/api/switchbot/status/{dev_id}/{dev_type}"
        try:
            r = requests.get(url, timeout=2.5)
            if r.status_code == 200:
                d = r.json().get("data", {})
                result[mid] = {
                    "watt": float(d["weight_w"]) if d.get("weight_w") is not None else None,
                    "volt": float(d["voltage_v"]) if d.get("voltage_v") is not None else None,
                }
            else:
                result[mid] = _PLUG_CACHE["data"].get(mid, {"watt": None, "volt": None})
        except Exception:
            result[mid] = _PLUG_CACHE["data"].get(mid, {"watt": None, "volt": None})

    _PLUG_CACHE["ts"] = now
    _PLUG_CACHE["data"] = result
    return result


def calc_breakeven_usd_h(watt: float) -> float:
    """消費電力(W)から1時間あたりの電気代($/h)を計算"""
    return ((watt / 1000.0) * ELECTRICITY_COST_JPY) / USD_JPY_RATE


def normalize_hourly_rate(raw_rate: float | None) -> float | None:
    """gRPCから24時間予測レート($/24h)が返ってきている場合は24で割って時給($/h)に補正"""
    if raw_rate is None:
        return None
    return (raw_rate / 24.0) if raw_rate > 0.8 else raw_rate


def fetch_system_status():
    """マシン・GPUフラグ・Salad/GPUメトリクス・SwitchBotプラグ実測値を一括取得"""
    try:
        r_m = requests.get(f"{API_BASE}/api/machine/list", timeout=3)
        machines = r_m.json() if r_m.status_code == 200 else []

        r_g = requests.get(f"{API_BASE}/api/gpu/list", timeout=3)
        gpus = r_g.json() if r_g.status_code == 200 else []

        r_s = requests.get(f"{API_BASE}/api/salad/status", timeout=4)
        salad_list = r_s.json() if r_s.status_code == 200 else []
        salad_map = {item["machine_id"]: item for item in salad_list}

        plug_metrics = fetch_all_plug_metrics()

        status_map = {}
        for m in machines:
            mid = m["machine_id"]
            s_info = salad_map.get(mid, {})
            p_info = plug_metrics.get(mid, {})
            status_map[mid] = {
                "power": m.get("power", "unknown"),
                "ip": m.get("ip_address", ""),
                "gpus": {},
                "salad": s_info.get("salad"),
                "plug_w": p_info.get("watt"),
                "plug_v": p_info.get("volt"),
            }

        for g in gpus:
            mid = g.get("machine_id")
            if mid in status_map:
                status_map[mid]["gpus"][g["gpu_name"]] = g.get("flag", "free")

        return status_map, "✅ 最新状態に更新しました"
    except Exception as e:
        return {}, f"❌ API通信エラー: {e}"


def format_summary_boxes(status_map: dict) -> tuple[str, str, str, str]:
    """下段上部の4つのサマリーボックス用Markdownを生成"""
    total_w = 0.0
    total_rate = 0.0
    total_net = 0.0
    balance = None
    last_24h = None
    plug_count = 0

    for mid, m_data in status_map.items():
        s = m_data.get("salad")
        plug_w = m_data.get("plug_w")
        is_on = (m_data.get("power") == "on")

        # 1. 消費電力の合算（プラグ実測値があれば優先、なければ推定値）
        node_w = 0.0
        if plug_w is not None:
            node_w = plug_w
            plug_count += 1
        elif s and is_on:
            node_w = s.get("power", {}).get("total_estimated_w", 0.0)
        total_w += node_w

        # 2. 時給レートと純利益の合算
        if s and is_on:
            earn = s.get("earnings", {})
            hourly_rate = normalize_hourly_rate(earn.get("current_rate_usd_h"))
            if hourly_rate is not None:
                total_rate += hourly_rate
                be = (
                    calc_breakeven_usd_h(node_w)
                    if plug_w is not None
                    else s.get("power", {}).get("breakeven_usd_h", 0.0)
                )
                total_net += (hourly_rate - be)

            if earn.get("balance_usd") is not None and balance is None:
                balance = earn["balance_usd"]
                last_24h = earn.get("last_24h_usd", 0.0)

    pwr_label = "🔌 総消費電力 (プラグ実測)" if plug_count > 0 else "⚡ 総消費電力 (推定)"
    box_power = f"#### {pwr_label}\n## {total_w:.1f} W"
    box_rate = f"#### 🥗 合計時給レート\n## ${total_rate:.4f} /h"

    net_icon = "🟢" if total_net >= 0 else "🔴"
    box_net = f"#### 📈 純利益 (電気代差引)\n## {net_icon} {total_net:+.4f} $/h"

    bal_str = f"${balance:.2f}" if balance is not None else "---"
    sub_str = f"(24h: ${last_24h:.2f})" if last_24h is not None else ""
    box_bal = f"#### 💰 Salad 残高 {sub_str}\n## {bal_str}"

    return box_power, box_rate, box_net, box_bal


def format_gpu_status_card(mid: str, gpu_name: str, m_data: dict) -> str:
    """下段のGPU別ステータスカード（4カラム）用のMarkdownを生成"""
    power = m_data.get("power", "unknown")
    flag = m_data.get("gpus", {}).get(gpu_name, "free")
    salad_data = m_data.get("salad")
    plug_w = m_data.get("plug_w")
    plug_v = m_data.get("plug_v")

    p_icon = "🟢 ON" if power == "on" else ("🔴 OFF" if power == "off" else "🟡 起動中")
    header = f"### 🎮 RTX {gpu_name} `[{mid}]`\n**電源:** {p_icon} ｜ **Flag:** `{flag.upper()}`\n\n---\n"

    key = GPU_METRIC_KEY.get(mid, {}).get(gpu_name, "target")
    m = None
    if salad_data and power == "on":
        m = salad_data.get("target_gpu") if key == "target" else salad_data.get("isolated_gpus", {}).get(key)

    plug_line = ""
    if plug_w is not None and key == "target":
        v_warn = "⚠️" if (plug_v is not None and plug_v < 95.0) else ""
        v_str = f" ({v_warn}`{plug_v:.1f} V`)" if plug_v is not None else ""
        plug_line = f"\n\n🔌 **PC実測:** `{plug_w:.1f} W`{v_str}"

    if m:
        w_val = m["power_w"]
        t_val = m["temp_c"]
        u_val = m["util_pct"]
        filled = min(10, max(0, round(u_val / 10)))
        bar = "🟩" * filled + "⬜" * (10 - filled)
        metrics_md = (
            f"**使用率:** `{u_val}%` {bar}\n\n"
            f"⚡ **GPU単体:** `{w_val:.1f} W` ｜ 🌡️ **温度:** `{t_val} °C`"
            f"{plug_line}\n\n---\n"
        )
    else:
        reason = "マシン停止中 (OFF)" if power != "on" else "Agent未応答 / データなし"
        metrics_md = f"**使用率:** `---`\n\n📡 *{reason}*{plug_line}\n\n---\n"

    if key != "target":
        salad_md = "🔒 **Salad:** 隔離GPU (SD / Ollama専用)"
    elif not salad_data or power != "on":
        salad_md = "🥗 **Salad:** 未接続"
    else:
        s_state = salad_data.get("state", "UNKNOWN")
        earn = salad_data.get("earnings", {})
        pwr = salad_data.get("power", {})
        hourly_rate = normalize_hourly_rate(earn.get("current_rate_usd_h"))
        be = calc_breakeven_usd_h(plug_w) if plug_w is not None else pwr.get("breakeven_usd_h", 0.0)

        if hourly_rate is not None:
            net_hourly = hourly_rate - be
            rate_str = f"${hourly_rate:.4f}/h"
            net_str = f"{net_hourly:+.4f}/h"
        else:
            rate_str = "---"
            net_str = f"分岐 ${be:.3f}/h"

        salad_md = f"🥗 **Salad:** `{s_state}`\n\n💵 **時給:** `{rate_str}` (`{net_str}`)"

    return header + metrics_md + salad_md


def set_machine_power(machine_id: str, target_power: str):
    url = f"{API_BASE}/api/machine/power"
    try:
        resp = requests.post(url, json={"machine_id": machine_id, "power": target_power}, timeout=5)
        if resp.status_code == 200:
            return f"🚀 {machine_id} の電源を '{target_power}' に要求しました"
        detail = resp.json().get("detail", resp.text)
        return f"⚠️ 電源操作エラー ({resp.status_code}): {detail}"
    except Exception as e:
        return f"❌ 通信失敗: {e}"


def update_gpu_flag(gpu_name: str, target_flag: str):
    url = f"{API_BASE}/api/gpu/update"
    try:
        resp = requests.post(url, json={"gpu_name": gpu_name, "flag": target_flag, "client_id": "ui"}, timeout=5)
        if resp.status_code == 200:
            return f"✨ {gpu_name} を '{target_flag}' に更新しました"
        detail = resp.json().get("detail", resp.text)
        return f"⚠️ フラグ更新エラー ({resp.status_code}): {detail}"
    except Exception as e:
        return f"❌ 通信失敗: {e}"


def toggle_salad_watchdog(machine_id: str, enabled: bool):
    url = f"{API_BASE}/api/salad/watchdog/{machine_id}"
    try:
        resp = requests.post(url, json={"enabled": bool(enabled)}, timeout=5)
        if resp.status_code == 200:
            state_str = "ON" if enabled else "OFF"
            return f"🛡️ {machine_id} の Salad 赤字自動停止を {state_str} に変更しました"
        detail = resp.json().get("detail", resp.text)
        return f"⚠️ Watchdog設定エラー ({resp.status_code}): {detail}"
    except Exception as e:
        return f"❌ 通信失敗: {e}"


def control_salad_app(machine_id: str, action: str):
    url = f"{API_BASE}/api/salad/app/{machine_id}/{action}"
    try:
        resp = requests.post(url, timeout=16)
        if resp.status_code == 200:
            act_str = "起動" if action == "start" else "完全停止"
            return f"🥗 {machine_id} の Salad アプリ本体を{act_str}しました"
        detail = resp.json().get("detail", resp.text)
        return f"⚠️ Saladアプリ操作エラー ({resp.status_code}): {detail}"
    except Exception as e:
        return f"❌ 通信失敗: {e}"


custom_css = """
.card-box {
    border: 1px solid var(--border-color-primary) !important;
    border-radius: 10px !important;
    padding: 14px !important;
    background: var(--block-background-fill) !important;
}
.summary-box {
    border: 1px solid var(--border-color-primary) !important;
    border-radius: 10px !important;
    padding: 12px 16px !important;
    background: var(--background-fill-secondary) !important;
}
"""

with gr.Blocks(title="Node & GPU Controller", theme=gr.themes.Soft(), css=custom_css) as demo:
    gr.Markdown("# 🎛️ Node & GPU Orchestration Dashboard")

    with gr.Row():
        refresh_btn = gr.Button("🔄 最新状態に手動更新", variant="primary", scale=3)
        auto_refresh = gr.Checkbox(label="30秒ごとに自動更新", value=False, scale=1)

    status_msg = gr.Markdown("待機中...")

    # =========================================================
    # 1. 上段: ノード＆GPU 操作パネル
    # =========================================================
    ui_components = {}
    with gr.Row():
        for node in NODES:
            mid = node["id"]
            with gr.Column(scale=1, min_width=300):
                with gr.Group(elem_classes=["card-box"]):
                    gr.Markdown(f"### 🖥️ {node['name']}")
                    power_label = gr.Markdown("**電源:** 取得中...")

                    with gr.Row():
                        btn_on = gr.Button("⚡ 起床 (WoL)", size="sm")
                        btn_off = gr.Button("💤 スリープ (S3)", size="sm")

                    btn_on.click(fn=lambda m=mid: set_machine_power(m, "on"), outputs=status_msg)
                    btn_off.click(fn=lambda m=mid: set_machine_power(m, "off"), outputs=status_msg)

                    gpu_radios = {}
                    for g in node["gpus"]:
                        radio = gr.Radio(
                            choices=FLAG_CHOICES,
                            label=f"🎮 GPU: {g}",
                            value="free",
                            interactive=True
                        )
                        radio.change(
                            fn=lambda val, target=g: update_gpu_flag(target, val),
                            inputs=radio,
                            outputs=status_msg
                        )
                        gpu_radios[g] = radio

                    with gr.Accordion("🥗 Salad 保守・Watchdog設定", open=False):
                        watchdog_chk = gr.Checkbox(
                            label="赤字時に自動一時停止 (Watchdog)",
                            value=False,
                            interactive=True
                        )
                        watchdog_chk.change(
                            fn=lambda val, m=mid: toggle_salad_watchdog(m, val),
                            inputs=watchdog_chk,
                            outputs=status_msg
                        )
                        with gr.Row():
                            btn_salad_start = gr.Button("🥗 本体起動", size="sm")
                            btn_salad_stop = gr.Button("🛑 完全停止", size="sm")

                        btn_salad_start.click(fn=lambda m=mid: control_salad_app(m, "start"), outputs=status_msg)
                        btn_salad_stop.click(fn=lambda m=mid: control_salad_app(m, "stop"), outputs=status_msg)

                    ui_components[mid] = {
                        "power_label": power_label,
                        "gpus": gpu_radios,
                        "watchdog_chk": watchdog_chk,
                    }

    # =========================================================
    # 2. 下段: ステータスモニター
    # =========================================================
    gr.Markdown("---")
    gr.Markdown("## 📊 Live Telemetry & Salad Earnings")

    # 2-A. クラスタ全体サマリー（4カラム）
    with gr.Row():
        with gr.Column(scale=1, min_width=180):
            with gr.Group(elem_classes=["summary-box"]):
                sum_power_md = gr.Markdown("#### ⚡ 総消費電力\n## --- W")
        with gr.Column(scale=1, min_width=180):
            with gr.Group(elem_classes=["summary-box"]):
                sum_rate_md = gr.Markdown("#### 🥗 合計時給レート\n## $--- /h")
        with gr.Column(scale=1, min_width=180):
            with gr.Group(elem_classes=["summary-box"]):
                sum_net_md = gr.Markdown("#### 📈 純利益\n## --- $/h")
        with gr.Column(scale=1, min_width=180):
            with gr.Group(elem_classes=["summary-box"]):
                sum_bal_md = gr.Markdown("#### 💰 Salad 残高\n## $---")

    # 2-B. GPU別ステータスカード（4枚横並び）
    gpu_status_cards = {}
    with gr.Row():
        for node in NODES:
            mid = node["id"]
            for g in node["gpus"]:
                with gr.Column(scale=1, min_width=240):
                    with gr.Group(elem_classes=["card-box"]):
                        card_md = gr.Markdown(f"### 🎮 RTX {g}\n取得中...")
                        gpu_status_cards[g] = card_md

    with gr.Row():
        gr.Button(
            "🥗 Salad Earn Summary (公式ダッシュボードを開く)",
            link="https://salad.com/earn/summary",
            variant="secondary",
            size="sm",
        )

    def refresh_ui():
        status_map, msg = fetch_system_status()
        updates = [msg]

        for node in NODES:
            mid = node["id"]
            m_data = status_map.get(mid, {})
            power = m_data.get("power", "unknown")
            salad_data = m_data.get("salad")
            g_map = m_data.get("gpus", {})
            plug_w = m_data.get("plug_w")
            plug_v = m_data.get("plug_v")

            p_icon = "🟢 ON" if power == "on" else ("🔴 OFF" if power == "off" else "🟡 移行中")
            v_str = f" / `{plug_v:.1f}V`" if plug_v is not None else ""
            plug_badge = f" ｜ 🔌 `{plug_w:.1f}W`{v_str}" if plug_w is not None else ""
            power_text = f"**電源:** {p_icon} (`{m_data.get('ip', 'N/A')}`){plug_badge}"
            updates.append(power_text)

            for g in node["gpus"]:
                updates.append(gr.update(value=g_map.get(g, "free")))

            wd_enabled = salad_data.get("auto_stop_enabled", False) if salad_data else False
            updates.append(gr.update(value=wd_enabled))

        b_pwr, b_rate, b_net, b_bal = format_summary_boxes(status_map)
        updates.extend([b_pwr, b_rate, b_net, b_bal])

        for node in NODES:
            mid = node["id"]
            m_data = status_map.get(mid, {})
            for g in node["gpus"]:
                updates.append(format_gpu_status_card(mid, g, m_data))

        return updates

    output_targets = [status_msg]
    for node in NODES:
        comp = ui_components[node["id"]]
        output_targets.append(comp["power_label"])
        for g in node["gpus"]:
            output_targets.append(comp["gpus"][g])
        output_targets.append(comp["watchdog_chk"])

    output_targets.extend([sum_power_md, sum_rate_md, sum_net_md, sum_bal_md])
    for node in NODES:
        for g in node["gpus"]:
            output_targets.append(gpu_status_cards[g])

    refresh_btn.click(fn=refresh_ui, outputs=output_targets)
    demo.load(fn=refresh_ui, outputs=output_targets)

    timer = gr.Timer(30, active=False)
    auto_refresh.change(lambda x: gr.Timer(active=x), inputs=auto_refresh, outputs=timer)
    timer.tick(fn=refresh_ui, outputs=output_targets)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7875)