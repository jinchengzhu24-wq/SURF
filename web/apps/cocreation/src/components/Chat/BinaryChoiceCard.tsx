import type { JSX } from "preact";
import { canChooseBinary } from "../../store/chatStore";
import { language } from "../../store/sessionStore";
import { submitBinaryChoice } from "../../store/workflows";
import type { BinaryQuestion, Turn } from "../../types";

export interface BinaryChoiceCardProps {
  question: BinaryQuestion;
  turn: Turn;
}

export function BinaryChoiceCard({ question, turn }: BinaryChoiceCardProps): JSX.Element {
  const canChoose = canChooseBinary(question.questionId) && question.status === "pending";
  const displayLanguage = question.sourceLanguage || turn.language || language.value;

  return (
    <div class="binary-discovery-card">
      <p class="binary-question">{question.question}</p>
      {question.note ? <p class="binary-note">{question.note}</p> : null}
      <div class="binary-choices">
        {(question.choices || []).map((choice) => {
          const selected = question.selectedChoiceId === choice.choiceId;
          return (
            <button
              key={choice.choiceId}
              type="button"
              class={`secondary-button binary-choice-button${selected ? " is-selected" : ""}`}
              aria-pressed={selected ? "true" : "false"}
              disabled={!canChoose}
              aria-disabled={!canChoose ? "true" : undefined}
              onClick={() => {
                if (!canChoose) return;
                submitBinaryChoice({
                  baseVersionId: question.baseVersionId,
                  topicId: question.topicId,
                  questionId: question.questionId,
                  choiceId: choice.choiceId,
                  displayLanguage,
                });
              }}
            >
              {`${choice.letter}: ${choice.label}`}
            </button>
          );
        })}
      </div>
    </div>
  );
}
