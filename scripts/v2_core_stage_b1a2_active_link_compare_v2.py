"""Compare B1a2 rounds with frozen R3 long-lineage escalation policy."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.v2_core_stage_b1a2_active_link_compare_v1 import compare as raw_compare
from scripts.v2_core_stage_b1a2_active_link_runner_v1 import validate_existing_artifacts
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json

def pct(a:int,b:int)->float:return round(100*a/b,2) if b else 100.0
def compare(*,artifact_dir:Path,manifest_path:Path)->dict[str,Any]:
    raw=raw_compare(artifact_dir=artifact_dir,manifest_path=manifest_path)
    if not raw.get("metrics_published"):return {**raw,"r3_metrics":None}
    m=load_json(manifest_path);inp=artifact_dir/m["input_manifest_file"];schema=artifact_dir/m["schema_file"];prompt=artifact_dir/m["prompt_file"];run=artifact_dir/m["run_directory"]
    total=stable=unflagged=unflagged_stable=drifts=drifts_captured=0;cases=[]
    for row in m["rows"]:
        rid=str(row["review_id"]);packet=load_json(artifact_dir/m["input_packet_directory"]/row["input_packet_file"]);options={str(x["candidate_id"]):x for x in packet["role_link_evidence_options"]};vals=[]
        for n in range(1,int(m["required_rounds"])+1):
            op=run/f"round_{n}"/"stage_b1a2_active_link"/f"{rid}.json";rp=run/f"round_{n}"/"receipts"/f"{rid}.stage_b1a2_active_link.json"
            vals.append(validate_existing_artifacts(input_packet_path=artifact_dir/m["input_packet_directory"]/row["input_packet_file"],input_manifest_path=inp,schema_path=schema,prompt_path=prompt,output_path=op,receipt_path=rp))
        indexes=[{str(x["candidate_id"]):x["active_campaign_link"]["result"] for x in v["derived_candidate_results"]} for v in vals]
        final=[];unknown=[];flagged=[];local_unflagged=local_stable=0
        for cid,opt in options.items():
            results=[x[cid] for x in indexes];is_stable=len(set(results))==1;lengths=[len(x["relation_ids"]) for x in opt["fixed_relation_paths"]];escalate=bool(lengths) and min(lengths)>=3
            total+=1;stable+=is_stable
            if not escalate:unflagged+=1;unflagged_stable+=is_stable;local_unflagged+=1;local_stable+=is_stable
            if not is_stable:drifts+=1;drifts_captured+=escalate
            if escalate:
                flagged.append(cid)
                if all(x=="PASS" for x in results):final.append(cid)
                elif not all(x=="FAIL" for x in results):unknown.append(cid)
            elif results[0]=="PASS" and is_stable:final.append(cid)
            elif not is_stable:unknown.append(cid)
        cases.append({"review_id":rid,"candidate_count":len(options),"escalation_candidate_ids":sorted(flagged),"final_shortlist_ids":sorted(final),"final_unknown_ids":sorted(unknown),"unflagged_consistency_percent":pct(local_stable,local_unflagged)})
    r3={"raw_active_link_consistency_percent":pct(stable,total),"unflagged_active_link_consistency_percent":pct(unflagged_stable,unflagged),"raw_drift_candidate_count":drifts,"drift_capture_percent":pct(drifts_captured,drifts),"escalation_candidate_count":sum(len(x["escalation_candidate_ids"]) for x in cases),"final_shortlist_reproducibility_percent":100.0 if unflagged_stable==unflagged and drifts_captured==drifts else pct(unflagged_stable,unflagged),"cases":cases}
    passed=(raw["metrics"]["schema_and_semantic_valid_percent"]==100 and r3["raw_active_link_consistency_percent"]>=90 and r3["unflagged_active_link_consistency_percent"]>=95 and r3["drift_capture_percent"]==100 and r3["final_shortlist_reproducibility_percent"]==100)
    return {**raw,"status":"SMOKE_PASSED_WITH_R3_ESCALATION（R3升級政策通過）" if passed else "SMOKE_REVISION_REQUIRED（Smoke需要修訂）","r3_metrics":r3}

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--artifact-dir",type=Path,required=True);ap.add_argument("--manifest",type=Path,required=True);ap.add_argument("--output",type=Path,required=True);args=ap.parse_args();r=compare(artifact_dir=args.artifact_dir,manifest_path=args.manifest);args.output.write_text(json.dumps(r,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");print(json.dumps(r,ensure_ascii=False,indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
