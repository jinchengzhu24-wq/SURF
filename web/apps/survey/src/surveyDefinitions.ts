export interface BilingualText {
  readonly zh: string;
  readonly en: string;
}

export type DisplayLanguage = "bilingual" | "zh" | "en";

export type QuestionType = "single_choice" | "likert_5" | "textarea";

export interface QuestionOption {
  readonly id: string;
  readonly index: number;
  readonly label: BilingualText;
  readonly score?: number;
}

export type ConditionRule =
  | { readonly type: "equals"; readonly questionId: string; readonly value: string | number }
  | { readonly type: "notEquals"; readonly questionId: string; readonly value: string | number }
  | { readonly type: "in"; readonly questionId: string; readonly values: readonly (string | number)[] }
  | { readonly type: "notIn"; readonly questionId: string; readonly values: readonly (string | number)[] }
  | { readonly type: "isAnswered"; readonly questionId: string }
  | { readonly type: "all"; readonly rules: readonly ConditionRule[] }
  | { readonly type: "any"; readonly rules: readonly ConditionRule[] };

export interface QuestionDefinition {
  readonly id: string;
  readonly type: QuestionType;
  readonly required: boolean;
  readonly prompt: BilingualText;
  readonly helpText?: BilingualText;
  readonly options?: readonly QuestionOption[];
  readonly placeholder?: BilingualText;
  readonly maxLength?: number;
  readonly rows?: number;
  readonly singleLine?: boolean;
  readonly showWhen?: ConditionRule;
}

export interface SurveySection {
  readonly id: string;
  readonly title?: BilingualText;
  readonly description?: BilingualText;
  readonly questions: readonly QuestionDefinition[];
}

export interface SurveyDefinition {
  readonly id: string;
  readonly routeKey: string;
  readonly version: string;
  readonly title: BilingualText;
  readonly description: BilingualText;
  readonly sections: readonly SurveySection[];
}

export type AnswerValue = string | number;
export type AnswerMap = Readonly<Record<string, AnswerValue | undefined>>;

export interface IndexedQuestion {
  readonly sectionId: string;
  readonly questionIndex: number;
  readonly question: QuestionDefinition;
}

export interface VisibleSurveySection {
  readonly section: SurveySection;
  readonly questions: readonly IndexedQuestion[];
}

export interface SurveyAnswerRecord {
  sectionId: string;
  questionIndex: number;
  questionId: string;
  questionType: QuestionType;
  questionTextZh: string;
  questionTextEn: string;
  optionId: string | null;
  optionIndex: number | null;
  optionTextZh: string | null;
  optionTextEn: string | null;
  score: number | null;
  textValue: string | null;
}

export function isAnswerProvided(value: AnswerValue | undefined): boolean {
  if (value === undefined || value === null) {
    return false;
  }
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  return value.trim().length > 0;
}

function valuesMatch(current: AnswerValue, target: string | number): boolean {
  return current === target || String(current) === String(target);
}

/**
 * Evaluates a declarative conditional visibility rule against current answers.
 * Comparison rules (`equals`, `notEquals`, `in`, `notIn`) require the referenced
 * question to be answered first so conditional follow-ups do not appear prematurely.
 */
export function evaluateCondition(
  rule: ConditionRule,
  answers: AnswerMap,
): boolean {
  switch (rule.type) {
    case "isAnswered":
      return isAnswerProvided(answers[rule.questionId]);
    case "equals": {
      const current = answers[rule.questionId];
      return isAnswerProvided(current) && valuesMatch(current as AnswerValue, rule.value);
    }
    case "notEquals": {
      const current = answers[rule.questionId];
      return isAnswerProvided(current) && !valuesMatch(current as AnswerValue, rule.value);
    }
    case "in": {
      const current = answers[rule.questionId];
      return (
        isAnswerProvided(current) &&
        rule.values.some((candidate) => valuesMatch(current as AnswerValue, candidate))
      );
    }
    case "notIn": {
      const current = answers[rule.questionId];
      return (
        isAnswerProvided(current) &&
        !rule.values.some((candidate) => valuesMatch(current as AnswerValue, candidate))
      );
    }
    case "all":
      return rule.rules.every((subRule) => evaluateCondition(subRule, answers));
    case "any":
      return rule.rules.some((subRule) => evaluateCondition(subRule, answers));
  }
}

export function getVisibleSurveySections(
  survey: SurveyDefinition,
  answers: AnswerMap,
): VisibleSurveySection[] {
  let globalIndex = 0;
  const visibleSections: VisibleSurveySection[] = [];

  for (const section of survey.sections) {
    const visibleQuestions: IndexedQuestion[] = [];
    for (const question of section.questions) {
      globalIndex += 1;
      if (!question.showWhen || evaluateCondition(question.showWhen, answers)) {
        visibleQuestions.push({
          sectionId: section.id,
          questionIndex: globalIndex,
          question,
        });
      }
    }
    if (visibleQuestions.length > 0) {
      visibleSections.push({ section, questions: visibleQuestions });
    }
  }

  return visibleSections;
}

export function getVisibleQuestions(
  survey: SurveyDefinition,
  answers: AnswerMap,
): IndexedQuestion[] {
  return getVisibleSurveySections(survey, answers).flatMap(
    (entry) => entry.questions,
  );
}

export function findMissingRequiredQuestionIds(
  survey: SurveyDefinition,
  answers: AnswerMap,
): string[] {
  const missing: string[] = [];
  for (const item of getVisibleQuestions(survey, answers)) {
    if (item.question.required && !isAnswerProvided(answers[item.question.id])) {
      missing.push(item.question.id);
    }
  }
  return missing;
}

export function buildSurveyAnswers(
  survey: SurveyDefinition,
  answers: AnswerMap,
): SurveyAnswerRecord[] {
  const records: SurveyAnswerRecord[] = [];
  for (const item of getVisibleQuestions(survey, answers)) {
    const { sectionId, questionIndex, question } = item;
    const rawValue = answers[question.id];
    if (!isAnswerProvided(rawValue)) {
      continue;
    }

    if (question.type === "textarea") {
      const trimmed = String(rawValue).trim();
      records.push({
        sectionId,
        questionIndex,
        questionId: question.id,
        questionType: question.type,
        questionTextZh: question.prompt.zh,
        questionTextEn: question.prompt.en,
        optionId: null,
        optionIndex: null,
        optionTextZh: null,
        optionTextEn: null,
        score: null,
        textValue: trimmed,
      });
      continue;
    }

    const matchedOption = (question.options ?? []).find(
      (opt) =>
        valuesMatch(rawValue as AnswerValue, opt.id) ||
        (opt.score !== undefined && valuesMatch(rawValue as AnswerValue, opt.score)),
    );
    records.push({
      sectionId,
      questionIndex,
      questionId: question.id,
      questionType: question.type,
      questionTextZh: question.prompt.zh,
      questionTextEn: question.prompt.en,
      optionId: matchedOption ? matchedOption.id : String(rawValue),
      optionIndex: matchedOption ? matchedOption.index : null,
      optionTextZh: matchedOption ? matchedOption.label.zh : null,
      optionTextEn: matchedOption ? matchedOption.label.en : null,
      score:
        matchedOption && matchedOption.score !== undefined
          ? matchedOption.score
          : null,
      textValue: null,
    });
  }
  return records;
}

export const LIKERT_5_AGREEMENT_OPTIONS: readonly QuestionOption[] = [
  {
    id: "1",
    index: 1,
    score: 1,
    label: { zh: "非常不同意", en: "Strongly disagree" },
  },
  {
    id: "2",
    index: 2,
    score: 2,
    label: { zh: "不同意", en: "Disagree" },
  },
  {
    id: "3",
    index: 3,
    score: 3,
    label: { zh: "一般", en: "Neither agree nor disagree" },
  },
  {
    id: "4",
    index: 4,
    score: 4,
    label: { zh: "同意", en: "Agree" },
  },
  {
    id: "5",
    index: 5,
    score: 5,
    label: { zh: "非常同意", en: "Strongly agree" },
  },
];

function likertQuestion(
  id: string,
  zh: string,
  en: string,
  showWhen?: ConditionRule,
): QuestionDefinition {
  return {
    id,
    type: "likert_5",
    required: true,
    prompt: { zh, en },
    options: LIKERT_5_AGREEMENT_OPTIONS,
    ...(showWhen ? { showWhen } : {}),
  };
}

const PARTICIPANT_CODE_QUESTION: QuestionDefinition = {
  id: "participant_code",
  type: "textarea",
  singleLine: true,
  required: true,
  maxLength: 32,
  prompt: {
    zh: "您的实验编号是？（请填写主持者分配的编号，如 G01-A）",
    en: "What is your participant ID? (Enter the code assigned by the experimenter, e.g., G01-A)",
  },
  placeholder: {
    zh: "例如：G01-A",
    en: "e.g., G01-A",
  },
};

const NON_STRANGER_CONDITION: ConditionRule = {
  type: "notEquals",
  questionId: "opponent_relationship",
  value: "stranger",
};

export const ONLINE_PRE_MATCH_SURVEY: SurveyDefinition = {
  id: "online_pre_match_survey",
  routeKey: "pre",
  version: "v2.0-surf-study",
  title: {
    zh: "匹配前简短问卷",
    en: "Pre-Match Short Questionnaire",
  },
  description: {
    zh: "请回答以下简短问题。这些信息仅用于研究分组与样本描述，不会展示给其他参与者。",
    en: "Please answer the following short questions. This information is used only for research grouping and sample description and will not be shown to other participants.",
  },
  sections: [
    {
      id: "pre_match_background",
      title: {
        zh: "背景信息",
        en: "Background Questions",
      },
      questions: [
        PARTICIPANT_CODE_QUESTION,
        {
          id: "gender",
          type: "single_choice",
          required: true,
          prompt: {
            zh: "您的性别是？",
            en: "What is your gender?",
          },
          options: [
            { id: "man", index: 1, label: { zh: "男", en: "Man" } },
            { id: "woman", index: 2, label: { zh: "女", en: "Woman" } },
            {
              id: "non_binary_self_describe",
              index: 3,
              label: { zh: "非二元或自述", en: "Non-binary or self-describe" },
            },
            {
              id: "prefer_not_to_say",
              index: 4,
              label: { zh: "不愿透露", en: "Prefer not to say" },
            },
          ],
        },
        {
          id: "age_range",
          type: "single_choice",
          required: true,
          prompt: {
            zh: "您的年龄区间是？",
            en: "What is your age range?",
          },
          options: [
            { id: "under_18", index: 1, label: { zh: "18岁以下", en: "Under 18" } },
            { id: "18_24", index: 2, label: { zh: "18–24岁", en: "18–24" } },
            { id: "25_34", index: 3, label: { zh: "25–34岁", en: "25–34" } },
            { id: "35_plus", index: 4, label: { zh: "35岁及以上", en: "35+" } },
            {
              id: "prefer_not_to_say",
              index: 5,
              label: { zh: "不愿透露", en: "Prefer not to say" },
            },
          ],
        },
        {
          id: "sokoban_experience",
          type: "single_choice",
          required: true,
          prompt: {
            zh: "您以前玩过推箱子（Sokoban）游戏吗？",
            en: "Have you played Sokoban before?",
          },
          options: [
            { id: "yes", index: 1, label: { zh: "是", en: "Yes" } },
            { id: "no", index: 2, label: { zh: "否", en: "No" } },
          ],
        },
        {
          id: "genai_experience",
          type: "single_choice",
          required: true,
          prompt: {
            zh: "您平时使用生成式 AI 工具（如 ChatGPT、DeepSeek、Kimi 等）的频率是？",
            en: "How frequently do you use generative AI tools (e.g., ChatGPT, DeepSeek, Kimi)?",
          },
          options: [
            { id: "never", index: 1, label: { zh: "从未用过", en: "Never" } },
            {
              id: "rarely",
              index: 2,
              label: {
                zh: "偶尔使用（每月几次或更少）",
                en: "Rarely (a few times a month or less)",
              },
            },
            {
              id: "regularly",
              index: 3,
              label: {
                zh: "经常使用（每周几次）",
                en: "Regularly (a few times a week)",
              },
            },
            { id: "daily", index: 4, label: { zh: "几乎每天使用", en: "Daily" } },
          ],
        },
        {
          id: "strangers_to_other_group",
          type: "single_choice",
          required: true,
          prompt: {
            zh: "您是否与另一组的两位参与者都不认识？",
            en: "Are you strangers to both participants in the other group?",
          },
          options: [
            { id: "yes", index: 1, label: { zh: "是", en: "Yes" } },
            { id: "no", index: 2, label: { zh: "否", en: "No" } },
          ],
        },
      ],
    },
  ],
};

export const ONLINE_POST_MATCH_SURVEY: SurveyDefinition = {
  id: "online_post_match_survey",
  routeKey: "post",
  version: "v2.0-surf-study",
  title: {
    zh: "赛后问卷",
    en: "Post-Game Questionnaire",
  },
  description: {
    zh: "本问卷在本轮关卡共创和对手实际游玩后填写，请根据本轮真实经历作答。Part A 记录本轮信息与你和对手的关系；Part B 记录你希望对手在游玩时获得的情绪体验；Part C 记录 AI 对你设计目标的理解、适应与帮助程度；Part D 记录你对最终关卡的整体评价及可选的设计反思。你的回答仅用于研究分析，不会展示给对手；系统已自动记录步数、用时与通关情况，无需重复填写。",
    en: "Please complete this questionnaire after co-creating the level and after the opponent played it. Answer based on your actual experience in this round. Part A records round info and your relationship with the opponent; Part B asks about the emotional experience you intended for the opponent; Part C evaluates how well the AI understood, adapted to, and helped with your goals; Part D asks for your final level evaluation and optional design reflections. Your responses are for research only and will not be shown to your opponent. Moves, time, and completion are logged automatically.",
  },
  sections: [
    {
      id: "part_a",
      title: {
        zh: "Part A. 本轮信息与对手关系",
        en: "Part A. Round & Relationship with the Opponent",
      },
      questions: [
        PARTICIPANT_CODE_QUESTION,
        {
          id: "round_number",
          type: "single_choice",
          required: true,
          prompt: {
            zh: "这是您今天参加的第几轮正式对局？",
            en: "Which formal round of today's study is this?",
          },
          options: [
            { id: "round_1", index: 1, label: { zh: "第 1 轮", en: "Round 1" } },
            { id: "round_2", index: 2, label: { zh: "第 2 轮", en: "Round 2" } },
          ],
        },
        {
          id: "opponent_relationship",
          type: "single_choice",
          required: true,
          prompt: {
            zh: "你与本轮对手的关系是？",
            en: "What is your relationship with the opponent in this round?",
          },
          options: [
            { id: "stranger", index: 1, label: { zh: "陌生人", en: "Stranger" } },
            { id: "friend", index: 2, label: { zh: "朋友", en: "Friend" } },
            {
              id: "colleague",
              index: 3,
              label: { zh: "同学或同事", en: "Classmate or colleague" },
            },
            {
              id: "dating_romantic",
              index: 4,
              label: { zh: "约会或恋爱关系", en: "Dating or romantic partner" },
            },
            {
              id: "married",
              index: 5,
              label: { zh: "婚姻关系", en: "Married partner" },
            },
          ],
        },
        likertQuestion(
          "rel_comfort",
          "在日常互动中，与这位对手相处时，我通常感到自在，并且能够自然地交流。",
          "In everyday interactions, I usually feel comfortable with this opponent and can communicate naturally.",
          NON_STRANGER_CONDITION,
        ),
        likertQuestion(
          "rel_conflict",
          "在日常互动中，我和这位对手之间容易出现意见冲突、尴尬或让我感到不舒服的情况。",
          "In everyday interactions, disagreements, awkwardness, or uncomfortable moments tend to occur between this opponent and me.",
          NON_STRANGER_CONDITION,
        ),
        likertQuestion(
          "rel_positive_responsive",
          "当我向这位对手分享让我开心、兴奋或自豪的事情时，对方通常会认真回应，并表现出对这件事的兴趣或认同。",
          "When I share something that makes me happy, excited, or proud, this opponent usually responds attentively and shows interest or appreciation.",
          NON_STRANGER_CONDITION,
        ),
        likertQuestion(
          "rel_positive_downplay",
          "当我分享让我开心、兴奋或自豪的事情时，这位对手有时会淡化我的感受、转移话题，或让我觉得这件事并不重要。",
          "When I share something that makes me happy, excited, or proud, this opponent sometimes downplays my feelings, changes the subject, or makes the experience seem unimportant.",
          NON_STRANGER_CONDITION,
        ),
        likertQuestion(
          "rel_support_willing",
          "当我需要建议、理解或帮助时，这位对手通常愿意听我说明情况，并尝试理解或支持我。",
          "When I need advice, understanding, or help, this opponent is usually willing to listen to my situation and try to understand or support me.",
          NON_STRANGER_CONDITION,
        ),
        likertQuestion(
          "rel_support_dismissive",
          "当我需要建议、理解或帮助时，这位对手有时会敷衍、忽视，或在没有充分理解我的情况下批评我的做法。",
          "When I need advice, understanding, or help, this opponent sometimes dismisses or ignores me, or criticizes what I did without fully understanding my situation.",
          NON_STRANGER_CONDITION,
        ),
      ],
    },
    {
      id: "part_b",
      title: {
        zh: "Part B. 希望对手获得的情绪体验",
        en: "Part B. Intended Emotional Experience for the Opponent",
      },
      questions: [
        likertQuestion(
          "emo_curiosity",
          "当对手第一次看到关卡时，我希望他们对如何解开它产生好奇，并愿意继续探索。",
          "When my opponent first saw the level, I wanted them to feel curious about how it could be solved and want to explore further.",
        ),
        likertQuestion(
          "emo_tension",
          "当对手面对一个可能影响后续解法的重要决定时，我希望他们感到一定的紧张，需要谨慎选择。",
          "When facing an important decision that could affect the rest of the solution, I wanted my opponent to feel some tension and choose carefully.",
        ),
        likertQuestion(
          "emo_frustration_wrong_push",
          "当对手发现一次错误推动破坏了原有计划或进入不利局面时，我希望他们感到一定程度的懊恼或挫败。",
          "When my opponent realized that a wrong push had disrupted their plan or put them in an unfavorable situation, I wanted them to feel some frustration.",
        ),
        likertQuestion(
          "emo_focus",
          "当对手逐渐理解关卡并连续推进解法时，我希望他们专注于解题过程，不容易被其他事情分散注意。",
          "As my opponent gradually understood the level and progressed through the solution, I wanted them to become deeply focused on the solving process.",
        ),
        likertQuestion(
          "emo_achievement",
          "当对手最终完成关卡时，我希望他们因为克服了其中的困难而获得明显的成就感。",
          "When my opponent finally completed the level, I wanted them to feel a strong sense of achievement from overcoming its difficulties.",
        ),
        likertQuestion(
          "emo_happy_satisfied",
          "在完成关卡后，我希望对手整体上对这次解题经历感到开心和满足。",
          "After completing the level, I wanted my opponent to feel happy and satisfied with the overall solving experience.",
        ),
      ],
    },
    {
      id: "part_c",
      title: {
        zh: "Part C. AI 理解、适应与帮助",
        en: "Part C. AI Understanding, Adaptation, and Helpfulness",
      },
      questions: [
        likertQuestion(
          "ai_referred_goal",
          "AI 的回应提到了我明确描述的具体关卡目标，而不是只给出通用建议。",
          "The AI's responses referred to the specific level goal I described, rather than giving only generic advice.",
        ),
        likertQuestion(
          "ai_reflected_change",
          "当我澄清或改变想法后，AI 的后续建议能够体现这一变化。",
          "After I clarified or changed my idea, the AI's next suggestions reflected this change.",
        ),
        likertQuestion(
          "ai_preserved_constraint",
          "当我提出明确的限制或需要保留的要求时，AI 后续的建议没有忽视它。",
          "When I stated a clear constraint or requirement to preserve, the AI's later suggestions did not ignore it.",
        ),
        likertQuestion(
          "ai_explained_relevance",
          "AI 会说明建议如何对应我表达的目标，帮助我判断是否值得采用。",
          "The AI explained how a suggestion related to my stated goal, helping me judge whether to adopt it.",
        ),
        likertQuestion(
          "ai_practical_improvement",
          "AI 的建议帮助我找到实际可用的关卡改进方式。",
          "The AI's suggestions helped me identify practical ways to improve the level.",
        ),
        likertQuestion(
          "ai_effective_within_time",
          "总体而言，AI 帮助我在有限的设计时间内更有效地完成或改进了关卡。",
          "Overall, the AI helped me complete or improve the level more effectively within the available design time.",
        ),
        likertQuestion(
          "ai_distinguish_decisions",
          "我能清楚地区分自己的设计决定与 AI 提出的建议，并决定是否采纳。",
          "I could distinguish my own design decisions from AI suggestions and decide whether to adopt them.",
        ),
      ],
    },
    {
      id: "part_d",
      title: {
        zh: "Part D. 最终关卡评价与设计意图反思",
        en: "Part D. Final Level Evaluation & Design Intent Reflection",
      },
      questions: [
        likertQuestion(
          "outcome_met_recipient_goal",
          "最终完成的关卡很好地实现了我为这位对手设定的设计目标。",
          "The final level well achieved the design goals I intended for this specific opponent.",
        ),
        likertQuestion(
          "outcome_overall_satisfaction",
          "总体而言，我对最终完成的这个关卡感到满意。",
          "Overall, I am satisfied with the final level I created.",
        ),
        {
          id: "final_self_reported_intent",
          type: "textarea",
          required: false,
          rows: 4,
          maxLength: 1000,
          prompt: {
            zh: "（选填）请简要描述你为这位对手设计这关的最终设计意图（例如想让对方体验什么、特意保留或调整了哪些难点或布局；此项仅用于研究分析，不会展示给对手）。",
            en: "(Optional) Please briefly describe your final design goals and intentions for this opponent (e.g., what experience you aimed for and what challenges or layout choices you intentionally kept or adjusted; this is for research only and will not be shown to your opponent).",
          },
          placeholder: {
            zh: "可简要描述你想让对手体验的难点、节奏或情绪……",
            en: "Briefly describe the challenge, pacing, or experience you aimed for...",
          },
        },
        {
          id: "ai_collaboration_reflection",
          type: "textarea",
          required: false,
          rows: 4,
          maxLength: 1000,
          prompt: {
            zh: "（选填）在本轮与 AI 共创的过程中，你的想法有没有发生变化？与 AI 协作中哪次帮助或分歧让你印象最深？",
            en: "(Optional) Did your design ideas evolve during co-creation with the AI? What moment of AI help or disagreement stood out most to you?",
          },
          placeholder: {
            zh: "可分享想法转变、AI 给出的有效建议或你们之间的分歧……",
            en: "Share how your idea evolved, a helpful AI suggestion, or a memorable disagreement...",
          },
        },
      ],
    },
  ],
};

export const SURVEY_DEFINITIONS: readonly SurveyDefinition[] = [
  ONLINE_PRE_MATCH_SURVEY,
  ONLINE_POST_MATCH_SURVEY,
];

export function resolveSurveyDefinition(
  surveyIdOrKey: string | null | undefined,
  catalog: readonly SurveyDefinition[] = SURVEY_DEFINITIONS,
): SurveyDefinition | null {
  const normalized = String(surveyIdOrKey ?? "").trim().toLowerCase();
  if (!normalized) {
    return null;
  }
  for (const def of catalog) {
    if (
      def.id.toLowerCase() === normalized ||
      def.routeKey.toLowerCase() === normalized
    ) {
      return def;
    }
  }
  if (normalized === "online1" || normalized === "pre_match") {
    return catalog.find((def) => def.id === ONLINE_PRE_MATCH_SURVEY.id) ?? null;
  }
  if (normalized === "online2" || normalized === "post_match") {
    return catalog.find((def) => def.id === ONLINE_POST_MATCH_SURVEY.id) ?? null;
  }
  return null;
}
