import type { Language } from "../types";

export interface RequestCopyBundle {
  readonly executeBoundProposal: string;
  readonly challengeRevision: string;
  readonly alternativeRevision: string;
  readonly manualEditSummary: string;
  readonly challengeChoiceAi: string;
  readonly challengeChoiceUser: string;
}

/**
 * Protocol-level request text sent to the backend on action clicks.
 * Strictly decoupled from presentation button labels in `en.json` / `zh-CN.json`.
 */
export const REQUEST_COPY: Readonly<Record<Language, RequestCopyBundle>> = Object.freeze({
  en: Object.freeze({
    executeBoundProposal: "Execute the bound proposal.",
    challengeRevision: "Challenge this plan",
    alternativeRevision: "Regenerate proposal",
    manualEditSummary: "Designer-saved map edit",
    challengeChoiceAi: "Yes",
    challengeChoiceUser: "No",
  }),
  "zh-CN": Object.freeze({
    executeBoundProposal: "执行已绑定的方案。",
    challengeRevision: "质疑这个方案",
    alternativeRevision: "重新生成方案",
    manualEditSummary: "设计者保存的地图修改",
    challengeChoiceAi: "是",
    challengeChoiceUser: "否",
  }),
});
