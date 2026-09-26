import gradio as gr
import requests
import json

# ミニPCの管理APIベースURL
API_BASE = "http://192.168.0.240:9000"

FLAG_CHOICES = ["free", "sd", "pending", "ritu_use", "ritu_sleep"]

def fetch_system_status():
    """マシンとGPUの状態をAPIから取得して辞書化"""
    try:
        # マシン一覧取得
        r_m = requests.get(f"{API_BASE}/api/machine/list", timeout=3)
        machines = r_m.json() if r_m.status_code == 200 else []

        # GPU一覧取得
        r_g = requests.get(f"{API_BASE}/api/gpu/list", timeout=3)
        gpus = r_g.json() if r_g.status_code == 200 else []

        # machine_id ごとにグループ化
        status_map = {}
        for m in machines:
            mid = m["machine_id"]
            status_map[mid] = {
                "power": m.get("power", "unknown"),
                "ip": m.get("ip_address", ""),
                "gpus": {}
            }

        for g in gpus:
            mid = g.get("machine_id")
            if mid in status_map:
                status_map[mid]["gpus"][g["gpu_name"]] = g.get("flag", "free")

        return status_map, "✅ 状態を更新しました"
    except Exception as e:
        return {}, f"❌ API通信エラー: {e}"

def set_machine_power(machine_id: str, target_power: str):
    """マシンの電源を変更 (on: WoL / off: S3)"""
    url = f"{API_BASE}/api/machine/power"
    try:
        resp = requests.post(url, json={"machine_id": machine_id, "power": target_power}, timeout=5)
        if resp.status_code == 200:
            return f"🚀 {machine_id} の電源を '{target_power}' に要求しました"
        else:
            detail = resp.json().get("detail", resp.text)
            return f"⚠️ 電源操作エラー ({resp.status_code}): {detail}"
    except Exception as e:
        return f"❌ 通信失敗: {e}"

def update_gpu_flag(gpu_name: str, target_flag: str):
    """GPUフラグを手動更新 (client_id='ui')"""
    url = f"{API_BASE}/api/gpu/update"
    try:
        resp = requests.post(url, json={"gpu_name": gpu_name, "flag": target_flag, "client_id": "ui"}, timeout=5)
        if resp.status_code == 200:
            return f"✨ {gpu_name} を '{target_flag}' に更新しました"
        else:
            detail = resp.json().get("detail", resp.text)
            return f"⚠️ フラグ更新エラー ({resp.status_code}): {detail}"
    except Exception as e:
        return f"❌ 通信失敗: {e}"

# --- カスタムCSS（スマホ表示の微調整） ---
custom_css = """
.machine-card {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 12px;
}
.power-badge-on { color: #16a34a; font-weight: bold; }
.power-badge-off { color: #dc2626; font-weight: bold; }
.power-badge-transitioning { color: #d97706; font-weight: bold; }
"""

with gr.Blocks(title="Node & GPU Controller", theme=gr.themes.Soft(), css=custom_css) as demo:
    gr.Markdown("# 🎛️ Node & GPU Orchestration Dashboard")

    with gr.Row():
        refresh_btn = gr.Button("🔄 最新状態に手動更新", variant="primary", scale=2)
        auto_refresh = gr.Checkbox(label="30秒ごとに自動更新", value=False, scale=1)

    status_msg = gr.Markdown("待機中...")

    # --- ノード別カードUI ---
    # 登録されているノード（torrent, terra, north）
    nodes = [
        {"id": "torrent", "name": "Torrent (Dual GPUサブ機)", "gpus": ["4070tis", "5060ti"]},
        {"id": "terra", "name": "Terra (常時稼働機)", "gpus": ["4060ti"]},
        {"id": "north", "name": "North (メイン機)", "gpus": ["5080"]}
    ]

    ui_components = {}

    with gr.Row():
        for node in nodes:
            mid = node["id"]
            with gr.Column(scale=1, min_width=300):
                with gr.Group(elem_classes=["machine-card"]):
                    gr.Markdown(f"### 🖥️ {node['name']}")
                    power_label = gr.Markdown("**電源:** 取得中...")
                    
                    with gr.Row():
                        btn_on = gr.Button("⚡ 起床 (WoL)", size="sm")
                        btn_off = gr.Button("💤 スリープ (S3)", size="sm")

                    btn_on.click(fn=lambda m=mid: set_machine_power(m, "on"), outputs=status_msg)
                    btn_off.click(fn=lambda m=mid: set_machine_power(m, "off"), outputs=status_msg)

                    gr.Markdown("---")
                    gr.Markdown("#### 🎮 搭載GPUフラグ")
                    
                    gpu_radios = {}
                    for g in node["gpus"]:
                        radio = gr.Radio(
                            choices=FLAG_CHOICES,
                            label=f"GPU: {g}",
                            value="free",
                            interactive=True
                        )
                        # フラグ変更イベント
                        radio.change(fn=lambda val, target=g: update_gpu_flag(target, val), inputs=radio, outputs=status_msg)
                        gpu_radios[g] = radio

                    ui_components[mid] = {
                        "power_label": power_label,
                        "gpus": gpu_radios
                    }

    def refresh_ui():
        status_map, msg = fetch_system_status()
        updates = []
        for node in nodes:
            mid = node["id"]
            m_data = status_map.get(mid, {})
            power = m_data.get("power", "unknown")
            
            # 電源ラベル更新
            badge_class = f"power-badge-{power}"
            power_text = f"**電源:** <span class='{badge_class}'>{power.upper()}</span> (IP: {m_data.get('ip', 'N/A')})"
            updates.append(power_text)

            # GPUフラグ更新
            g_map = m_data.get("gpus", {})
            for g in node["gpus"]:
                current_flag = g_map.get(g, "free")
                updates.append(gr.update(value=current_flag))

        return [msg] + updates

    # 出力リストの構築
    output_targets = [status_msg]
    for node in nodes:
        output_targets.append(ui_components[node["id"]]["power_label"])
        for g in node["gpus"]:
            output_targets.append(ui_components[node["id"]]["gpus"][g])

    # イベント紐付け
    refresh_btn.click(fn=refresh_ui, outputs=output_targets)
    demo.load(fn=refresh_ui, outputs=output_targets)

    # タイマー更新（自動更新チェック時用）
    timer = gr.Timer(30, active=False)
    auto_refresh.change(lambda x: gr.Timer(active=x), inputs=auto_refresh, outputs=timer)
    timer.tick(fn=refresh_ui, outputs=output_targets)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7875)