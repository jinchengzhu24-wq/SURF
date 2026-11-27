import { NoticeBanner } from "@sokoban/ui-core";
import type { JSX } from "preact";
import { useState } from "preact/hooks";
import type { EntrySource } from "../surveyContext";
import type { DisplayLanguage, SurveyDefinition } from "../surveyDefinitions";
import type { SubmissionReceipt } from "../surveyStorage";
import { BilingualCopy } from "./QuestionCard";

export interface SurveyReceiptProps {
  survey: SurveyDefinition;
  receipt: SubmissionReceipt;
  alreadySubmitted: boolean;
  entrySource: EntrySource;
  language: DisplayLanguage;
  onCloseTab: () => void;
  onFillAnother?: () => void;
  onBackToLanding?: () => void;
}

export function SurveyReceipt({
  survey,
  receipt,
  alreadySubmitted,
  entrySource,
  language,
  onCloseTab,
  onFillAnother,
  onBackToLanding,
}: SurveyReceiptProps): JSX.Element {
  const [closeFallbackShown, setCloseFallbackShown] = useState(false);

  const handleCloseClick = () => {
    onCloseTab();
    setCloseFallbackShown(true);
  };

  const bannerMessage = alreadySubmitted
    ? language === "zh"
      ? "检测到本轮问卷已提交，已为您防止重复提交。"
      : language === "en"
        ? "This questionnaire was already submitted. Duplicate submission has been prevented."
        : "检测到本轮问卷已提交，已防止重复提交。 / Already submitted — duplicate submission prevented."
    : language === "zh"
      ? "问卷已成功提交并保存！"
      : language === "en"
        ? "Your survey response has been submitted and saved!"
        : "问卷已成功提交并保存！ / Survey response submitted and saved!";

  const primaryMessage = alreadySubmitted
    ? {
        zh: "您此前已提交过本问卷，无需重复提交。您可以关闭此页面返回游戏继续流程。",
        en: "You have already submitted this survey. No duplicate submission is needed. You may close this tab and return to the game.",
      }
    : {
        zh: "问卷已提交！您可以关闭此页面返回游戏继续流程。",
        en: "Survey submitted! You may close this tab and return to the game.",
      };

  return (
    <section class="stone-panel receipt-panel" aria-live="polite">
      <div class="stone-panel-header">
        <div>
          <p class="panel-eyebrow">
            {alreadySubmitted ? "ALREADY SUBMITTED" : "SUBMISSION CONFIRMED"}
          </p>
          <h2>
            <BilingualCopy
              text={
                alreadySubmitted
                  ? { zh: "已完成提交", en: "Already Submitted" }
                  : { zh: "提交成功", en: "Submission Complete" }
              }
              language={language}
            />
          </h2>
        </div>
        <span class="header-pill">
          {entrySource === "unity_matchmaking" ? "STUDY" : "TEST"}
        </span>
      </div>

      <div class="receipt-body">
        <NoticeBanner
          message={bannerMessage}
          variant="success"
          durationMs={null}
          className="receipt-banner"
        />

        <p class="receipt-lead">
          <BilingualCopy text={primaryMessage} language={language} />
        </p>

        <dl class="receipt-meta">
          <div class="receipt-meta-row">
            <dt>
              <BilingualCopy
                text={{ zh: "问卷", en: "Survey" }}
                language={language}
              />
            </dt>
            <dd>
              <BilingualCopy text={survey.title} language={language} />
            </dd>
          </div>
          <div class="receipt-meta-row">
            <dt>
              <BilingualCopy
                text={{ zh: "提交时间 (UTC)", en: "Submitted At (UTC)" }}
                language={language}
              />
            </dt>
            <dd class="mono-value">{receipt.submittedAt}</dd>
          </div>
          <div class="receipt-meta-row">
            <dt>
              <BilingualCopy
                text={{ zh: "回执编号", en: "Response ID" }}
                language={language}
              />
            </dt>
            <dd class="mono-value">{receipt.responseId}</dd>
          </div>
        </dl>

        {closeFallbackShown ? (
          <NoticeBanner
            message={
              language === "zh"
                ? "如果浏览器未自动关闭标签页，请手动关闭当前标签页并返回游戏。"
                : language === "en"
                  ? "If your browser did not close this tab automatically, please close it manually and return to the game."
                  : "如果浏览器未自动关闭标签页，请手动关闭当前标签页并返回游戏。 / If this tab did not close automatically, please close it manually and return to the game."
            }
            variant="warning"
            durationMs={null}
            className="receipt-close-hint"
          />
        ) : null}

        <div class="receipt-actions">
          <button
            type="button"
            class="pixel-btn pixel-btn--play"
            onClick={handleCloseClick}
          >
            <BilingualCopy
              text={{ zh: "关闭此页面", en: "Close Tab" }}
              language={language}
              className="btn-bilingual"
            />
          </button>

          {entrySource === "standalone" && onFillAnother ? (
            <button
              type="button"
              class="pixel-btn pixel-btn--secondary"
              onClick={onFillAnother}
            >
              <BilingualCopy
                text={{ zh: "再填一份测试问卷", en: "Fill Another Test Response" }}
                language={language}
                className="btn-bilingual"
              />
            </button>
          ) : null}

          {entrySource === "standalone" && onBackToLanding ? (
            <button
              type="button"
              class="pixel-btn pixel-btn--secondary"
              onClick={onBackToLanding}
            >
              <BilingualCopy
                text={{ zh: "返回问卷列表", en: "Switch Survey" }}
                language={language}
                className="btn-bilingual"
              />
            </button>
          ) : null}
        </div>
      </div>
    </section>
  );
}
