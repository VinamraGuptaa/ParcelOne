import { apiPost, type WorkflowResponse, type WorkflowSummary } from '../api/client';

export type RestartWorkflowInput = Pick<
  WorkflowSummary,
  | 'district_label'
  | 'taluka_label'
  | 'village_label'
  | 'survey_part1'
  | 'survey_option_label'
  | 'owner_name'
>;

export async function cancelWorkflow(workflowId: string): Promise<WorkflowResponse> {
  return apiPost<WorkflowResponse>(`/workflows/${workflowId}/cancel`, {});
}

export async function restartLandCaseWorkflow(
  wf: RestartWorkflowInput,
): Promise<WorkflowResponse> {
  return apiPost<WorkflowResponse>('/workflows/land-case-search', {
    district_label: wf.district_label,
    taluka_label: wf.taluka_label,
    village_label: wf.village_label,
    survey_part1: wf.survey_part1,
    survey_option_label: wf.survey_option_label,
    owner_name: wf.owner_name,
  });
}

export function parseActiveWorkflowId(detail: unknown): string | null {
  if (
    typeof detail === 'object' &&
    detail !== null &&
    'active_workflow_id' in detail &&
    typeof (detail as { active_workflow_id: unknown }).active_workflow_id === 'string'
  ) {
    return (detail as { active_workflow_id: string }).active_workflow_id;
  }
  return null;
}
