import gradio as gr
import pandas as pd
import os
import sys
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Dict, Any, List, Generator, Optional, Tuple
import plotly.graph_objects as go
import numpy as np
import requests
from dataclasses import dataclass

# --- Constants and Mappings ---
MODEL_MAPPING = {
    "ESM-1v": "esm1v", "ESM2-650M": "esm2", "SaProt": "saprot",
    "ESM-IF1": "esmif1", "MIF-ST": "mifst", "ProSST-2048": "prosst",
    "ProSSN": "protssn","ESM-1b":"esm1b"
}
AI_MODELS = {
    "DeepSeek": {
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat"
    }
}

# --- AI & Helper Functions ---
@dataclass
class AIConfig:
    api_key: str; model_name: str; api_base: str; model: str

def get_api_key(ai_provider: str, user_input_key: str = "") -> Optional[str]:
    if user_input_key and user_input_key.strip(): return user_input_key.strip()
    env_var_map = {"DeepSeek": "DEEPSEEK_API_KEY"}
    env_var_name = env_var_map.get(ai_provider)
    if env_var_name and os.getenv(env_var_name): return os.getenv(env_var_name)
    return None

def call_ai_api(config: AIConfig, prompt: str) -> str:
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    data = {"model": config.model, "messages": [{"role": "system", "content": "You are an expert protein scientist..."}, {"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 2000}
    try:
        response = requests.post(f"{config.api_base}/chat/completions", headers=headers, json=data, timeout=60)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ API call failed: {str(e)}"

def generate_mutation_ai_prompt(results_df: pd.DataFrame, model_name: str) -> Optional[str]:
    """
    Generates an optimized and professional prompt for mutation analysis based on a pre-sorted DataFrame.

    This function takes a DataFrame assumed to be sorted from most to least beneficial mutation.
    It robustly identifies the mutation and score columns, extracts the top and bottom 10%,
    and constructs a structured prompt for in-depth biological analysis.

    Args:
        results_df (pd.DataFrame): DataFrame with prediction results, sorted by score.
                                   It should contain a mutation column (e.g., 'A1F') and a score column.
        model_name (str): The name of the model used for the prediction.

    Returns:
        A well-structured prompt string for the AI model, or None if data is invalid.
    """
    # --- Robust Column Identification ---
    # Find mutant column: prefer 'mutant', fallback to the first column.
    if 'mutant' in results_df.columns:
        mutant_col = 'mutant'
    elif len(results_df.columns) > 0:
        mutant_col = results_df.columns[0]
    else:
        print("Error: DataFrame has no columns. Cannot generate AI summary.")
        return None

    # Find score column: prefer a name containing 'score', fallback to the second column.
    score_col = next((col for col in results_df.columns if 'score' in col.lower()), None)
    if not score_col:
        if len(results_df.columns) > 1:
            score_col = results_df.columns[1]
        else:
            print("Error: Could not find a score column. Cannot generate AI summary.")
            return None

    # Determine the number of rows for the top and bottom 5%
    num_rows = len(results_df)
    if num_rows < 5:  # Handle very small dataframes
        top_count = num_rows
        lowest_count = 0 # No "bottom" if all results are shown as "top"
    else:
        top_count = max(1, int(num_rows * 0.05))
        lowest_count = max(1, int(num_rows * 0.05))

    # The DataFrame is already sorted from most beneficial to least beneficial
    top_mutations = results_df.head(top_count)
    lowest_mutations = results_df.tail(lowest_count) if lowest_count > 0 else pd.DataFrame()

    # Format the filtered data into clean string tables using the identified column names
    top_mutations_str = top_mutations[[mutant_col, score_col]].to_string(index=False)
    lowest_mutations_str = lowest_mutations[[mutant_col, score_col]].to_string(index=False) if not lowest_mutations.empty else "N/A"
    
    # Construct the structured prompt with clearer context
    prompt = f"""
Please act as an expert protein engineer and analyze the following mutation prediction results generated by the '{model_name}' model.

A deep mutational scan was performed. The results are sorted from most beneficial to least beneficial based on the '{score_col}' (a zero-shot score). Below are the most significant findings: the top 5% and the bottom 5% of mutations.

### Top 5% Predicted Mutations (Potentially Most Beneficial):
```
{top_mutations_str}
```

### Bottom 5% Predicted Mutations (Potentially Most Detrimental):
```
{lowest_mutations_str}
```

### Your Analysis Task:
Based on this data, provide a structured scientific analysis report that includes the following sections:

1.  **Executive Summary**: Briefly summarize the key findings. Are there clear hotspot regions for beneficial mutations?
2.  **Analysis of Beneficial Mutations**: Discuss the top mutations. Are there specific residues or regions that show potential as hotspots for improvement? What biochemical properties might these mutations be altering (e.g., improving protein packing, removing unfavorable charges)?
3.  **Analysis of Detrimental Mutations & Sequence Conservation**: Discuss the mutations predicted to be most harmful. What do these positions tell us about sequence conservation and functionally critical residues? Positions that are highly intolerant to mutation are likely essential for the protein's structure or function.
4.  **Recommendations for Experimentation**: Based on your analysis, suggest 3-5 specific point mutations that are the most promising candidates for experimental validation in the lab. Please justify your choices.

Please provide a concise, clear, and insightful report in a professional scientific tone suitable for biologists.
"""
    return prompt

def run_zero_shot_prediction(model_type: str, model_name: str, file_path: str) -> Tuple[str, pd.DataFrame]:
    try:
        output_csv = f"temp_{model_type}_{int(time.time())}.csv"
        script_path = f"src/mutation/models/{MODEL_MAPPING[model_name]}.py"
        file_argument = "--pdb_file" if model_type == "structure" else "--fasta_file"
        cmd = [sys.executable, script_path, file_argument, file_path, "--output_csv", output_csv]
        subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore')
        if os.path.exists(output_csv):
            return "Prediction completed successfully!", pd.read_csv(output_csv)
        return "Prediction completed but no output file was generated.", pd.DataFrame()
    except Exception as e:
        error_detail = e.stderr if isinstance(e, subprocess.CalledProcessError) else str(e)
        return f"Prediction failed: {error_detail}", pd.DataFrame()

def prepare_plotly_heatmap_data(df: pd.DataFrame, max_residues: int = None) -> Tuple:
    score_col = next((col for col in df.columns if 'score' in col.lower()), None)
    if score_col is None: return (None,) * 6
    valid_mutations_df = df[df['mutant'].apply(lambda m: isinstance(m, str) and len(m) > 2 and m[0] != m[-1] and m[1:-1].isdigit())].copy()
    if valid_mutations_df.empty: return ([], [], np.array([[]]), np.array([[]]), np.array([[]]), score_col)
    valid_mutations_df['rank'] = valid_mutations_df[score_col].rank(method='min', ascending=False).astype(int)
    valid_mutations_df['inverted_rank_bin'] = 11 - np.ceil(valid_mutations_df['rank'] / (len(valid_mutations_df) / 10)).clip(upper=10)
    valid_mutations_df['position'] = valid_mutations_df['mutant'].str[1:-1].astype(int)
    sorted_positions = sorted(valid_mutations_df['position'].unique())
    if max_residues is not None:
        sorted_positions = sorted_positions[:max_residues]
        valid_mutations_df = valid_mutations_df[valid_mutations_df['position'].isin(sorted_positions)]
    x_labels = list("ACDEFGHIKLMNPQRSTVWY")
    x_map = {lbl: i for i, lbl in enumerate(x_labels)}
    wt_map = {pos: mut[0] for pos, mut in zip(valid_mutations_df['position'], valid_mutations_df['mutant'])}
    y_labels = [f"{wt_map.get(pos, '?')}{pos}" for pos in sorted_positions]
    y_map = {pos: i for i, pos in enumerate(sorted_positions)}
    z_data, rank_matrix, score_matrix = (np.full((len(y_labels), len(x_labels)), np.nan) for _ in range(3))
    for _, row in valid_mutations_df.iterrows():
        pos, mut_aa = row['position'], row['mutant'][-1]
        if pos in y_map and mut_aa in x_map:
            y_idx, x_idx = y_map[pos], x_map[mut_aa]
            z_data[y_idx, x_idx], rank_matrix[y_idx, x_idx], score_matrix[y_idx, x_idx] = row['inverted_rank_bin'], row['rank'], round(row[score_col], 3)
    return x_labels, y_labels, z_data, rank_matrix, score_matrix, score_col

def generate_plotly_heatmap(x_labels: List, y_labels: List, z_data: np.ndarray, rank_data: np.ndarray, score_data: np.ndarray, is_partial: bool = False, total_residues: int = None) -> go.Figure:
    if z_data is None or z_data.size == 0: return go.Figure().update_layout(title="No data to display")
    num_residues = len(y_labels)
    height_base = 150 if is_partial else 200
    dynamic_height = max(400, min(8000, 30 * num_residues + height_base))
    custom_data = np.stack((rank_data, score_data), axis=-1)
    fig = go.Figure(data=go.Heatmap(
        z=z_data, 
        x=x_labels, 
        y=y_labels, 
        customdata=custom_data, 
        hovertemplate="<b>Position</b>: %{y}<br><b>Mutation to</b>: %{x}<br><b>Rank</b>: %{customdata[0]}<br><b>Score</b>: %{customdata[1]}<extra></extra>", 
        colorscale='RdYlGn_r', 
        zmin=1, 
        zmax=10, 
        showscale=True, 
        colorbar={'title': 'Rank Percentile', 'tickvals': [10, 6, 1], 'ticktext': ['Top 10%', 'Top 50%', 'Lowest 10%']}))
    title_text = "Prediction Heatmap"
    if is_partial and total_residues and total_residues > len(y_labels): title_text += f" (Showing first {num_residues} of {total_residues} residues)"
    fig.update_layout(title=title_text, xaxis_title='Mutant Amino Acid', yaxis_title='Residue Position', height=dynamic_height, yaxis_autorange='reversed')
    return fig

def get_total_residues_count(df: pd.DataFrame) -> int:
    if df.empty: return 0
    valid = df[df['mutant'].apply(lambda m: isinstance(m, str) and len(m) > 2 and m[0] != m[-1] and m[1:-1].isdigit())].copy()
    if valid.empty: return 0
    return valid['mutant'].str[1:-1].astype(int).nunique()

def create_zip_archive(files_to_zip: Dict[str, str], zip_filename: str) -> str:
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zf:
        for src, arc in files_to_zip.items():
            if os.path.exists(src): zf.write(src, arcname=arc)
    return zip_filename

def create_zero_shot_tab(constant: Dict[str, Any]) -> Dict[str, Any]:
    sequence_models = list(constant.get("zero_shot_sequence_model", {}).keys())
    structure_models = list(constant.get("zero_shot_structure_model", {}).keys())

    def parse_fasta_file(file_path: str) -> str:
        if not file_path: return ""
        try:
            with open(file_path, 'r') as f: return "".join([l.strip() for l in f if not l.startswith('>')])
        except Exception as e: return f"Error: {e}"
    
    def parse_pdb_for_sequence(file_path: str) -> str:
        aa_code_map = {'ALA': 'A', 'CYS': 'C', 'ASP': 'D', 'GLU': 'E', 'PHE': 'F', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I', 'LYS': 'K', 'LEU': 'L', 'MET': 'M', 'ASN': 'N', 'PRO': 'P', 'GLN': 'Q', 'ARG': 'R', 'SER': 'S', 'THR': 'T', 'VAL': 'V', 'TRP': 'W', 'TYR': 'Y'}
        sequence, seen_residues, current_chain = [], set(), None
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    if line.startswith("ATOM"):
                        chain_id = line[21]
                        if current_chain is None: current_chain = chain_id
                        if chain_id != current_chain: break
                        res_name, res_seq_num = line[17:20].strip(), int(line[22:26])
                        residue_id = (chain_id, res_seq_num)
                        if residue_id not in seen_residues:
                            if res_name in aa_code_map:
                                sequence.append(aa_code_map[res_name])
                                seen_residues.add(residue_id)
            return "".join(sequence)
        except Exception as e:
            return "Error: Could not read sequence from PDB file."

    def display_protein_sequence_from_fasta(file_obj: Any) -> str:
        return parse_fasta_file(file_obj.name) if file_obj else ""
    
    def display_protein_sequence_from_pdb(file_obj: Any) -> str:
        return parse_pdb_for_sequence(file_obj.name) if file_obj else ""
    
    def toggle_ai_section(enable_ai: bool):
        return gr.update(visible=enable_ai)

    def handle_prediction_with_ai(model_type: str, model_name: str, file_obj: Any, enable_ai: bool, ai_model: str, user_api_key: str) -> Generator:
        if not model_name or not file_obj:
            yield "❌ Error: Model and file are required.", None, None, gr.update(visible=False), None, gr.update(visible=False), None, "Please select a model and upload a file."
            return

        yield f"⏳ Running {model_type} prediction...", None, None, gr.update(visible=False), None, gr.update(visible=False), None, "Prediction in progress..."
        status, df = run_zero_shot_prediction(model_type, model_name, file_obj.name)
        
        if df.empty:
            yield status, go.Figure(layout={'title': 'No results generated'}), pd.DataFrame(), gr.update(visible=False), None, gr.update(visible=False), None, "No results to analyze."
            return
        
        total_residues = get_total_residues_count(df)
        data_tuple = prepare_plotly_heatmap_data(df, max_residues=40)
        
        if data_tuple[0] is None:
            yield status, go.Figure(layout={'title': 'Score column not found'}), df, gr.update(visible=False), None, gr.update(visible=False), df, "Score column not found."
            return

        # --- FIX: Unpack only the first 5 items for the plot ---
        plot_data = data_tuple[:5]
        summary_fig = generate_plotly_heatmap(*plot_data, is_partial=True, total_residues=total_residues)
        
        ai_summary = "AI analysis was not enabled."
        if enable_ai:
            yield f"✅ Prediction complete. 🤖 Generating AI summary...", summary_fig, df, gr.update(visible=False), None, gr.update(visible=total_residues > 40), df, "🤖 AI is analyzing the results..."
            api_key = get_api_key(ai_model, user_api_key)
            if not api_key:
                ai_summary = "❌ No API key found. Please provide one or set the environment variable."
            else:
                ai_config = AIConfig(api_key, ai_model, AI_MODELS[ai_model]["api_base"], AI_MODELS[ai_model]["model"])
                prompt = generate_mutation_ai_prompt(df, model_name)
                ai_summary = call_ai_api(ai_config, prompt)
        
        # --- FIX: Unpack only the first 5 items for the saved plot ---
        full_data_tuple = prepare_plotly_heatmap_data(df)
        full_plot_data = full_data_tuple[:5]
        full_fig = generate_plotly_heatmap(*full_plot_data, is_partial=False, total_residues=total_residues)
        
        temp_dir = Path("temp_outputs"); temp_dir.mkdir(exist_ok=True)
        run_timestamp = int(time.time())
        csv_path = temp_dir / f"temp_{model_type}_results_{run_timestamp}.csv"
        df.to_csv(csv_path, index=False)
        heatmap_path = temp_dir / f"temp_{model_type}_heatmap_{run_timestamp}.html"
        full_fig.write_html(heatmap_path)
        
        files_to_zip = {str(csv_path): "prediction_results.csv", str(heatmap_path): "prediction_heatmap.html"}
        if not ai_summary.startswith("❌") and not ai_summary.startswith("AI analysis was not enabled"):
            report_path = temp_dir / f"temp_ai_report_{run_timestamp}.md"
            with open(report_path, 'w', encoding='utf-8') as f: f.write(ai_summary)
            files_to_zip[str(report_path)] = "AI_Analysis_Report.md"

        zip_path = temp_dir / f"prediction_{model_type}_results_{run_timestamp}.zip"
        create_zip_archive(files_to_zip, str(zip_path))

        final_status = status if not enable_ai else "✅ Prediction and AI analysis complete!"
        yield final_status, summary_fig, df, gr.update(visible=True, value=str(zip_path)), str(zip_path), gr.update(visible=total_residues > 40), df, ai_summary

    def expand_heatmap(full_df):
        if full_df is None or full_df.empty: return go.Figure(), gr.update(visible=True), gr.update(visible=False)
        # --- FIX: Unpack only the first 5 items ---
        data_tuple = prepare_plotly_heatmap_data(full_df)
        plot_data = data_tuple[:5]
        fig = generate_plotly_heatmap(*plot_data, is_partial=False, total_residues=get_total_residues_count(full_df))
        return fig, gr.update(visible=False), gr.update(visible=True)

    def collapse_heatmap(full_df):
        if full_df is None or full_df.empty: return go.Figure(), gr.update(visible=True), gr.update(visible=False)
        # --- FIX: Unpack only the first 5 items ---
        data_tuple = prepare_plotly_heatmap_data(full_df, max_residues=40)
        plot_data = data_tuple[:5]
        fig = generate_plotly_heatmap(*plot_data, is_partial=True, total_residues=get_total_residues_count(full_df))
        return fig, gr.update(visible=True), gr.update(visible=False)

    with gr.Tabs():
        # --- SEQUENCE TAB ---
        with gr.TabItem("🧬 Sequence-based Model"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=2):
                    gr.Markdown("### Model Configuration")
                    seq_model_dd = gr.Dropdown(choices=sequence_models, label="Select Sequence-based Model", value=sequence_models[0] if sequence_models else None)
                    seq_file_upload = gr.File(label="Upload FASTA file", file_types=[".fasta", ".fa"], type="filepath")
                    seq_protein_display = gr.Textbox(label="Uploaded Protein Sequence", interactive=False, lines=5, max_lines=10)
                    with gr.Accordion("AI Analysis (Optional)", open=True):
                        seq_enable_ai = gr.Checkbox(label="Enable AI Summary", value=False)
                        with gr.Group(visible=False) as seq_ai_box:
                            seq_ai_model_dd = gr.Dropdown(choices=list(AI_MODELS.keys()), value="DeepSeek", label="Select AI Model")
                            seq_api_key_in = gr.Textbox(label="API Key (Optional)", type="password", placeholder="Leave blank for env var")
                    seq_predict_btn = gr.Button("🚀 Start Prediction", variant="primary")
                with gr.Column(scale=3):
                    gr.Markdown("### Prediction Results")
                    seq_status_box = gr.Textbox(label="Status", interactive=False)
                    with gr.Tabs():
                        with gr.TabItem("📈 Prediction Heatmap"):
                            with gr.Row(visible=False) as seq_view_controls:
                                seq_expand_btn = gr.Button("Show Complete Heatmap", variant="secondary", size="sm")
                                seq_collapse_btn = gr.Button("Show Summary View", variant="secondary", size="sm", visible=False)
                            seq_plot_out = gr.Plot(label="Heatmap")
                        with gr.TabItem("📊 Raw Results"):
                            seq_df_out = gr.DataFrame(label="Raw Data")
                        with gr.TabItem("🤖 AI Analysis"):
                            seq_ai_out = gr.Textbox(
                                label="AI Analysis Report",
                                value="AI analysis will appear here...",
                                lines=20, 
                                interactive=False, 
                                show_copy_button=True 
                            )
                    seq_download_btn = gr.DownloadButton("💾 Download Results", visible=False)

        # --- STRUCTURE TAB ---
        with gr.TabItem("🏗️ Structure-based Model"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=2):
                    gr.Markdown("### Model Configuration")
                    struct_model_dd = gr.Dropdown(choices=structure_models, label="Select Structure-based Model", value=structure_models[0] if structure_models else None)
                    struct_file_upload = gr.File(label="Upload PDB file", file_types=[".pdb"], type="filepath")
                    struct_protein_display = gr.Textbox(label="Uploaded Protein Sequence", interactive=False, lines=5, max_lines=10)
                    with gr.Accordion("AI Analysis (Optional)", open=True):
                        struct_enable_ai = gr.Checkbox(label="Enable AI Summary", value=False)
                        with gr.Group(visible=False) as struct_ai_box:
                            struct_ai_model_dd = gr.Dropdown(choices=list(AI_MODELS.keys()), value="DeepSeek", label="Select AI Model")
                            struct_api_key_in = gr.Textbox(label="API Key (Optional)", type="password", placeholder="Leave blank for env var")
                    struct_predict_btn = gr.Button("🚀 Start Prediction", variant="primary")
                with gr.Column(scale=3):
                    gr.Markdown("### Prediction Results")
                    struct_status_box = gr.Textbox(label="Status", interactive=False)
                    with gr.Tabs():
                        with gr.TabItem("📈 Prediction Heatmap"):
                            with gr.Row(visible=False) as struct_view_controls:
                                struct_expand_btn = gr.Button("Show Complete Heatmap", variant="secondary", size="sm")
                                struct_collapse_btn = gr.Button("Show Summary View", variant="secondary", size="sm", visible=False)
                            struct_plot_out = gr.Plot(label="Heatmap")
                        with gr.TabItem("📊 Raw Results"):
                            struct_df_out = gr.DataFrame(label="Raw Data")
                        with gr.TabItem("🤖 AI Analysis"):
                            struct_ai_out = gr.Textbox(
                                label="AI Analysis Report",
                                value="AI analysis will appear here...",
                                lines=20, 
                                interactive=False, 
                                show_copy_button=True 
                            )
                    struct_download_btn = gr.DownloadButton("💾 Download Results", visible=False)

    # --- State variables and Event Handlers ---
    seq_full_data_state = gr.State()
    struct_full_data_state = gr.State()
    
    seq_file_upload.upload(fn=display_protein_sequence_from_fasta, inputs=seq_file_upload, outputs=seq_protein_display)
    seq_enable_ai.change(fn=toggle_ai_section, inputs=seq_enable_ai, outputs=seq_ai_box)
    seq_predict_btn.click(
        fn=handle_prediction_with_ai, 
        inputs=[gr.State("sequence"), seq_model_dd, seq_file_upload, seq_enable_ai, seq_ai_model_dd, seq_api_key_in], 
        outputs=[seq_status_box, seq_plot_out, seq_df_out, seq_download_btn, gr.State(), seq_view_controls, seq_full_data_state, seq_ai_out]
    )
    seq_expand_btn.click(fn=expand_heatmap, inputs=[seq_full_data_state], outputs=[seq_plot_out, seq_expand_btn, seq_collapse_btn])
    seq_collapse_btn.click(fn=collapse_heatmap, inputs=[seq_full_data_state], outputs=[seq_plot_out, seq_expand_btn, seq_collapse_btn])

    struct_file_upload.upload(fn=display_protein_sequence_from_pdb, inputs=struct_file_upload, outputs=struct_protein_display)
    struct_enable_ai.change(fn=toggle_ai_section, inputs=struct_enable_ai, outputs=struct_ai_box)
    struct_predict_btn.click(
        fn=handle_prediction_with_ai, 
        inputs=[gr.State("structure"), struct_model_dd, struct_file_upload, struct_enable_ai, struct_ai_model_dd, struct_api_key_in], 
        outputs=[struct_status_box, struct_plot_out, struct_df_out, struct_download_btn, gr.State(), struct_view_controls, struct_full_data_state, struct_ai_out]
    )
    struct_expand_btn.click(fn=expand_heatmap, inputs=[struct_full_data_state], outputs=[struct_plot_out, struct_expand_btn, struct_collapse_btn])
    struct_collapse_btn.click(fn=collapse_heatmap, inputs=[struct_full_data_state], outputs=[struct_plot_out, struct_expand_btn, struct_collapse_btn])

    return {}
