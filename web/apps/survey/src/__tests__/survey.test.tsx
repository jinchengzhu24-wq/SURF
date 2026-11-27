import {
  cleanup,
  fireEvent,
  render,
  waitFor,
  within,
} from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, buildSurveyUrlWithType } from "../App";
import { parseSurveyLaunchContext } from "../surveyContext";
import {
  ONLINE_POST_MATCH_SURVEY,
  ONLINE_PRE_MATCH_SURVEY,
  evaluateCondition,
  type SurveyDefinition,
} from "../surveyDefinitions";
import { buildSubmittedStorageKey } from "../surveyStorage";

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function createMemoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.get(key) ?? null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(key);
    },
    setItem(key: string, value: string) {
      store.set(key, String(value));
    },
  };
}

function createFakeLocation(search: string) {
  return {
    href: `https://sokobanaidemo.top/survey/${search}`,
    origin: "https://sokobanaidemo.top",
    protocol: "https:",
    hostname: "sokobanaidemo.top",
    pathname: "/survey/",
    search,
  };
}

describe("declarative survey definitions & condition engine", () => {
  it("defines v2.0-surf-study Online1 (6 questions) and Online2 (26 questions across Parts A, B, C, D)", () => {
    expect(ONLINE_PRE_MATCH_SURVEY.id).toBe("online_pre_match_survey");
    expect(ONLINE_PRE_MATCH_SURVEY.version).toBe("v2.0-surf-study");
    const preQuestions = ONLINE_PRE_MATCH_SURVEY.sections.flatMap(
      (section) => section.questions,
    );
    expect(preQuestions).toHaveLength(6);
    expect(preQuestions.map((q) => q.id)).toEqual([
      "participant_code",
      "gender",
      "age_range",
      "sokoban_experience",
      "genai_experience",
      "strangers_to_other_group",
    ]);
    expect(preQuestions[0]).toMatchObject({
      type: "textarea",
      singleLine: true,
      required: true,
      maxLength: 32,
    });
    expect(preQuestions[1].options).toHaveLength(4);
    expect(preQuestions[4].options).toHaveLength(4);

    expect(ONLINE_POST_MATCH_SURVEY.id).toBe("online_post_match_survey");
    expect(ONLINE_POST_MATCH_SURVEY.version).toBe("v2.0-surf-study");
    expect(ONLINE_POST_MATCH_SURVEY.sections.map((s) => s.id)).toEqual([
      "part_a",
      "part_b",
      "part_c",
      "part_d",
    ]);
    expect(ONLINE_POST_MATCH_SURVEY.sections[0].questions).toHaveLength(9);
    expect(ONLINE_POST_MATCH_SURVEY.sections[1].questions).toHaveLength(6);
    expect(ONLINE_POST_MATCH_SURVEY.sections[2].questions).toHaveLength(7);
    expect(ONLINE_POST_MATCH_SURVEY.sections[3].questions).toHaveLength(4);
    const postQuestions = ONLINE_POST_MATCH_SURVEY.sections.flatMap(
      (section) => section.questions,
    );
    expect(postQuestions).toHaveLength(26);
    expect(postQuestions.some((q) => q.id === "emo_frustration_disrupted_plan")).toBe(
      false,
    );
    expect(
      ONLINE_POST_MATCH_SURVEY.sections[0].questions
        .slice(3)
        .every(
          (q) =>
            q.type === "likert_5" &&
            q.showWhen?.type === "notEquals" &&
            q.showWhen.questionId === "opponent_relationship" &&
            q.showWhen.value === "stranger",
        ),
    ).toBe(true);
    expect(ONLINE_POST_MATCH_SURVEY.sections[3].questions[2]).toMatchObject({
      id: "final_self_reported_intent",
      type: "textarea",
      required: false,
    });
    expect(ONLINE_POST_MATCH_SURVEY.sections[3].questions[3]).toMatchObject({
      id: "ai_collaboration_reflection",
      type: "textarea",
      required: false,
    });
  });

  it("evaluates equals, notEquals, in, notIn, isAnswered, all, and any rules", () => {
    const answers = { role: "designer", rating: 4, note: "clear layout" };

    expect(
      evaluateCondition(
        { type: "equals", questionId: "role", value: "designer" },
        answers,
      ),
    ).toBe(true);
    expect(
      evaluateCondition(
        { type: "notEquals", questionId: "role", value: "observer" },
        answers,
      ),
    ).toBe(true);
    expect(
      evaluateCondition(
        { type: "notEquals", questionId: "missing", value: "observer" },
        answers,
      ),
    ).toBe(false);
    expect(
      evaluateCondition(
        { type: "in", questionId: "rating", values: [4, 5] },
        answers,
      ),
    ).toBe(true);
    expect(
      evaluateCondition(
        { type: "notIn", questionId: "rating", values: [1, 2] },
        answers,
      ),
    ).toBe(true);
    expect(
      evaluateCondition({ type: "isAnswered", questionId: "note" }, answers),
    ).toBe(true);
    expect(
      evaluateCondition(
        { type: "isAnswered", questionId: "note" },
        { note: "   " },
      ),
    ).toBe(false);
    expect(
      evaluateCondition(
        {
          type: "all",
          rules: [
            { type: "equals", questionId: "role", value: "designer" },
            { type: "in", questionId: "rating", values: [4, 5] },
          ],
        },
        answers,
      ),
    ).toBe(true);
    expect(
      evaluateCondition(
        {
          type: "any",
          rules: [
            { type: "equals", questionId: "role", value: "other" },
            { type: "equals", questionId: "rating", value: 4 },
          ],
        },
        answers,
      ),
    ).toBe(true);
  });

  it("parses query routing, hidden tracking params, and entrySource accurately", () => {
    const landing = parseSurveyLaunchContext(createFakeLocation(""));
    expect(landing.surveyId).toBeNull();
    expect(landing.entrySource).toBe("standalone");
    expect(landing.playerNumber).toBe(0);

    const preByAlias = parseSurveyLaunchContext(
      createFakeLocation("?survey=pre"),
    );
    expect(preByAlias.surveyId).toBe("online_pre_match_survey");
    expect(preByAlias.entrySource).toBe("standalone");

    const postById = parseSurveyLaunchContext(
      createFakeLocation(
        "?surveyId=online_post_match_survey&source=unity&studySessionId=sid-123&matchId=mid-456&roomCode=ROOM77&playerNumber=2&participantId=P09&condition=collab",
      ),
    );
    expect(postById.surveyId).toBe("online_post_match_survey");
    expect(postById.entrySource).toBe("unity_matchmaking");
    expect(postById.studySessionId).toBe("sid-123");
    expect(postById.matchId).toBe("mid-456");
    expect(postById.roomCode).toBe("ROOM77");
    expect(postById.playerNumber).toBe(2);
    expect(postById.participantId).toBe("P09");
    expect(postById.experimentCondition).toBe("collab");

    const implicitUnity = parseSurveyLaunchContext(
      createFakeLocation("?type=pre&studySessionId=sid-only&playerNumber=9"),
    );
    expect(implicitUnity.entrySource).toBe("unity_matchmaking");
    expect(implicitUnity.playerNumber).toBe(0);
  });
});

describe("@sokoban/survey App UI and workflows", () => {
  it("renders a bilingual landing selector at bare /survey/ and navigates to chosen survey", async () => {
    const pushState = vi.fn();
    const fakeWindow = {
      history: { pushState, replaceState: vi.fn() },
      close: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };

    const { getByRole, getByText } = render(
      <App
        locationObj={createFakeLocation("")}
        windowObj={fakeWindow}
        storage={createMemoryStorage()}
      />,
    );

    expect(getByText(/匹配前简短问卷/)).toBeTruthy();
    expect(getByText(/Pre-Match Short Questionnaire/)).toBeTruthy();
    expect(getByText(/赛后问卷/)).toBeTruthy();
    expect(getByText(/Post-Game Questionnaire/)).toBeTruthy();

    fireEvent.click(
      getByRole("button", { name: /匹配前简短问卷|Pre-Match Short Questionnaire/i }),
    );

    expect(getByText("您的性别是？")).toBeTruthy();
    expect(getByText("What is your gender?")).toBeTruthy();
    expect(pushState).toHaveBeenCalled();
  });

  it("renders Pre-Match (?type=pre) with 6 questions, single-line participant_code, language toggle, validation, standalone submit & reset, and zero studySessionId inputs", async () => {
    const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
    const fetchMock: typeof fetch = vi.fn(async (input, init) => {
      requests.push({
        url: String(input),
        body: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      });
      return jsonResponse({
        status: "ok",
        id: 101,
        recordType: "survey",
        eventType: "survey-response",
        deduplicated: false,
      });
    });

    let currentMs = 1000;
    let uuidCounter = 0;
    const storage = createMemoryStorage();

    const { container, getByRole, getByText, queryByText } = render(
      <App
        locationObj={createFakeLocation("?type=pre")}
        fetchImpl={fetchMock}
        storage={storage}
        nowMs={() => currentMs}
        idGenerator={() => `resp-standalone-${++uuidCounter}`}
      />,
    );

    const getProgressText = () =>
      container.querySelector(".progress-count")?.textContent?.trim() ?? "";

    // Exactly 1 single-line text input (participant_code) and zero inputs for hidden tracking fields
    const textInputs = container.querySelectorAll('input[type="text"]');
    expect(textInputs).toHaveLength(1);
    expect((textInputs[0] as HTMLInputElement).name).toBe("q_participant_code");
    expect(container.querySelectorAll("fieldset")).toHaveLength(6);
    expect(getProgressText()).toBe("0 / 6");
    // Internal surveyVersion must never be shown in visible participant UI
    expect(queryByText(/v2\.0-surf-study/i)).toBeNull();
    expect(container.textContent ?? "").not.toContain("v2.0-surf-study");

    // Language toggle switches display between bilingual, zh, and en
    fireEvent.click(getByRole("button", { name: "中文" }));
    expect(getByText("您的性别是？")).toBeTruthy();
    expect(queryByText("What is your gender?")).toBeNull();

    fireEvent.click(getByRole("button", { name: "EN" }));
    expect(getByText("What is your gender?")).toBeTruthy();
    expect(queryByText("您的性别是？")).toBeNull();

    fireEvent.click(getByRole("button", { name: "双语" }));
    expect(getByText("您的性别是？")).toBeTruthy();
    expect(getByText("What is your gender?")).toBeTruthy();

    // Submitting with missing required questions shows validation error and does not POST
    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelectorAll(".question-card--error")).toHaveLength(6);

    // Complete all 6 required questions
    const fieldsets = Array.from(container.querySelectorAll("fieldset"));
    fireEvent.input(within(fieldsets[0]).getByRole("textbox"), {
      target: { value: "  G01-A  " },
    });
    fireEvent.click(within(fieldsets[1]).getByRole("radio", { name: /女/ }));
    fireEvent.click(
      within(fieldsets[2]).getByRole("radio", { name: /18–24岁/ }),
    );
    fireEvent.click(within(fieldsets[3]).getByRole("radio", { name: /是/ }));
    fireEvent.click(within(fieldsets[4]).getByRole("radio", { name: /经常使用/ }));
    fireEvent.click(within(fieldsets[5]).getByRole("radio", { name: /否/ }));

    expect(getProgressText()).toBe("6 / 6");

    currentMs = 15500;
    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));

    await waitFor(() => {
      expect(requests).toHaveLength(1);
    });

    expect(requests[0].url).toBe("/record-survey-response");
    const payload = requests[0].body;
    expect(payload.eventType).toBe("survey-response");
    expect(payload.surveyId).toBe("online_pre_match_survey");
    expect(payload.surveyVersion).toBe("v2.0-surf-study");
    expect(payload.entrySource).toBe("standalone");
    expect(payload.responseId).toBe("resp-standalone-1");
    expect(payload.participantId).toBe("G01-A");
    expect(payload.studySessionId).toBe("");
    expect(payload.sessionId).toBe("");
    expect(payload.matchId).toBe("");
    expect(payload.roomCode).toBe("");
    expect(payload.playerNumber).toBe(0);
    expect(payload.durationSeconds).toBe(14.5);
    expect(payload.answers).toHaveLength(6);
    expect((payload.answers as Array<Record<string, unknown>>)[0]).toMatchObject(
      {
        questionIndex: 1,
        questionId: "participant_code",
        questionType: "textarea",
        optionId: null,
        optionIndex: null,
        score: null,
        textValue: "G01-A",
      },
    );
    expect((payload.answers as Array<Record<string, unknown>>)[1]).toMatchObject(
      {
        questionIndex: 2,
        questionId: "gender",
        questionType: "single_choice",
        questionTextZh: "您的性别是？",
        questionTextEn: "What is your gender?",
        optionId: "woman",
        optionIndex: 2,
        optionTextZh: "女",
        optionTextEn: "Woman",
        score: null,
        textValue: null,
      },
    );

    // Confirmation screen shown, standalone mode does NOT lock localStorage
    await waitFor(() => {
      expect(
        getByText(/问卷已提交！您可以关闭此页面返回游戏继续流程。/),
      ).toBeTruthy();
    });
    expect(storage.length).toBe(0);

    // Standalone reset button clears answers and generates fresh responseId
    const resetBtn = getByRole("button", {
      name: /再填一份测试问卷|Fill Another Test Response/i,
    });
    fireEvent.click(resetBtn);
    expect(container.querySelectorAll("fieldset")).toHaveLength(6);
    expect(getProgressText()).toBe("0 / 6");
  });

  it("renders Post-Game (?type=post) across Parts A/B/C/D, reveals Part A Likert items for non-stranger, submits Unity hidden params, and blocks duplicates across remounts via localStorage", async () => {
    const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
    const fetchMock: typeof fetch = vi.fn(async (input, init) => {
      requests.push({
        url: String(input),
        body: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      });
      return jsonResponse({
        status: "ok",
        id: 202,
        recordType: "survey",
        eventType: "survey-response",
        deduplicated: false,
      });
    });

    const storage = createMemoryStorage();
    const unitySearch =
      "?type=post&source=unity&studySessionId=study-u1&matchId=match-99&roomCode=AB12CD&playerNumber=2";
    const fakeLocation = createFakeLocation(unitySearch);

    const { container, getByRole, getByText, queryByRole, unmount } = render(
      <App
        locationObj={fakeLocation}
        fetchImpl={fetchMock}
        storage={storage}
        idGenerator={() => "resp-unity-post-1"}
      />,
    );

    // 1 single-line text input (participant_code), zero hidden tracking inputs, internal version hidden
    const textInputs = container.querySelectorAll('input[type="text"]');
    expect(textInputs).toHaveLength(1);
    expect((textInputs[0] as HTMLInputElement).name).toBe("q_participant_code");
    expect(container.textContent ?? "").not.toContain("v2.0-surf-study");

    // Verify 4 sections initially (Part A shows 3 base questions while opponent_relationship unselected -> 20 visible)
    const sections = Array.from(container.querySelectorAll(".survey-section"));
    expect(sections).toHaveLength(4);
    expect(sections[0].textContent).toContain("Part A. 本轮信息与对手关系");
    expect(sections[0].textContent).toContain("你与本轮对手的关系是？");
    expect(sections[0].querySelectorAll("fieldset")).toHaveLength(3);

    expect(sections[1].textContent).toContain("Part B. 希望对手获得的情绪体验");
    expect(sections[1].textContent).toContain(
      "当对手第一次看到关卡时，我希望他们对如何解开它产生好奇，并愿意继续探索。",
    );
    expect(sections[1].querySelectorAll("fieldset")).toHaveLength(6);

    expect(sections[2].textContent).toContain("Part C. AI 理解、适应与帮助");
    expect(sections[2].textContent).toContain(
      "AI 的回应提到了我明确描述的具体关卡目标，而不是只给出通用建议。",
    );
    expect(sections[2].querySelectorAll("fieldset")).toHaveLength(7);

    expect(sections[3].textContent).toContain("Part D. 最终关卡评价与设计意图反思");
    expect(sections[3].textContent).toContain(
      "最终完成的关卡很好地实现了我为这位对手设定的设计目标。",
    );
    expect(sections[3].querySelectorAll("fieldset")).toHaveLength(4);
    expect(container.querySelectorAll("fieldset")).toHaveLength(20);

    // Selecting a non-stranger relationship ("朋友") reveals all 6 Part A Likert items -> 26 total
    const initialFieldsets = Array.from(container.querySelectorAll("fieldset"));
    fireEvent.input(within(initialFieldsets[0]).getByRole("textbox"), {
      target: { value: "G01-B" },
    });
    fireEvent.click(within(initialFieldsets[1]).getByRole("radio", { name: /第 1 轮/ }));
    fireEvent.click(within(initialFieldsets[2]).getByRole("radio", { name: /朋友/ }));

    expect(sections[0].querySelectorAll("fieldset")).toHaveLength(9);

    // Verify DOM ordering across Parts A, B, C, D
    const fullText = container.textContent ?? "";
    const posPartA = fullText.indexOf("Part A. 本轮信息与对手关系");
    const posQ1 = fullText.indexOf("您的实验编号是？");
    const posPartB = fullText.indexOf("Part B. 希望对手获得的情绪体验");
    const posQ10 = fullText.indexOf("当对手第一次看到关卡时");
    const posPartC = fullText.indexOf("Part C. AI 理解、适应与帮助");
    const posQ16 = fullText.indexOf("AI 的回应提到了我明确描述的具体关卡目标");
    const posPartD = fullText.indexOf("Part D. 最终关卡评价与设计意图反思");
    const posQ23 = fullText.indexOf("最终完成的关卡很好地实现了我为这位对手设定的设计目标");
    expect(posPartA).toBeGreaterThan(-1);
    expect(posPartA).toBeLessThan(posQ1);
    expect(posQ1).toBeLessThan(posPartB);
    expect(posPartB).toBeLessThan(posQ10);
    expect(posQ10).toBeLessThan(posPartC);
    expect(posPartC).toBeLessThan(posQ16);
    expect(posQ16).toBeLessThan(posPartD);
    expect(posPartD).toBeLessThan(posQ23);

    // Answer all 21 Likert sliders (6 Part A + 6 Part B + 7 Part C + 2 Part D); leave 2 optional textareas blank
    const fieldsets = Array.from(container.querySelectorAll("fieldset"));
    expect(fieldsets).toHaveLength(26);
    for (const fs of fieldsets) {
      const slider = within(fs).queryByRole("slider") as HTMLInputElement | null;
      if (!slider) {
        continue;
      }
      expect(
        fs.querySelector('.likert-slider[data-answered="false"]'),
      ).toBeTruthy();
      fireEvent.input(slider, { target: { value: "4" } }); // score 4 (同意 / Agree)
    }

    expect(container.querySelector(".progress-count")?.textContent?.trim()).toBe(
      "24 / 26",
    );

    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));

    await waitFor(() => {
      expect(requests).toHaveLength(1);
    });

    const payload = requests[0].body;
    expect(payload.surveyId).toBe("online_post_match_survey");
    expect(payload.surveyVersion).toBe("v2.0-surf-study");
    expect(payload.entrySource).toBe("unity_matchmaking");
    expect(payload.participantId).toBe("G01-B");
    expect(payload.studySessionId).toBe("study-u1");
    expect(payload.sessionId).toBe("study-u1");
    expect(payload.matchId).toBe("match-99");
    expect(payload.roomCode).toBe("AB12CD");
    expect(payload.playerNumber).toBe(2);
    expect(payload.responseId).toBe("resp-unity-post-1");
    const answers = payload.answers as Array<Record<string, unknown>>;
    expect(answers).toHaveLength(24);
    expect(answers[0]).toMatchObject({
      sectionId: "part_a",
      questionIndex: 1,
      questionId: "participant_code",
      questionType: "textarea",
      textValue: "G01-B",
    });
    expect(answers[1]).toMatchObject({
      sectionId: "part_a",
      questionIndex: 2,
      questionId: "round_number",
      optionId: "round_1",
      optionIndex: 1,
    });
    expect(answers[2]).toMatchObject({
      sectionId: "part_a",
      questionIndex: 3,
      questionId: "opponent_relationship",
      optionId: "friend",
      optionIndex: 2,
      score: null,
    });
    expect(answers[9]).toMatchObject({
      sectionId: "part_b",
      questionIndex: 10,
      questionId: "emo_curiosity",
      optionId: "4",
      optionIndex: 4,
      optionTextZh: "同意",
      optionTextEn: "Agree",
      score: 4,
    });
    expect(answers[15]).toMatchObject({
      sectionId: "part_c",
      questionIndex: 16,
      questionId: "ai_referred_goal",
      optionId: "4",
      optionIndex: 4,
      optionTextZh: "同意",
      optionTextEn: "Agree",
      score: 4,
    });
    expect(answers[22]).toMatchObject({
      sectionId: "part_d",
      questionIndex: 23,
      questionId: "outcome_met_recipient_goal",
      optionId: "4",
      optionIndex: 4,
      score: 4,
    });

    // Formal mode does NOT show "Fill Another Test Response" reset button
    await waitFor(() => {
      expect(
        getByText(/Survey submitted! You may close this tab and return to the game\./),
      ).toBeTruthy();
    });
    expect(
      queryByRole("button", {
        name: /再填一份测试问卷|Fill Another Test Response/i,
      }),
    ).toBeNull();

    // Receipt persisted under deterministic localStorage key
    const expectedKey = buildSubmittedStorageKey(
      "online_post_match_survey",
      "study-u1",
      "match-99",
    );
    expect(storage.getItem(expectedKey)).toBeTruthy();

    // Remounting with the same Unity URL immediately shows Already Submitted receipt & blocks duplicate submit
    unmount();
    const remount = render(
      <App
        locationObj={fakeLocation}
        fetchImpl={fetchMock}
        storage={storage}
        idGenerator={() => "resp-unity-post-2"}
      />,
    );
    expect(remount.container.querySelectorAll("fieldset")).toHaveLength(0);
    expect(remount.getByText(/resp-unity-post-1/)).toBeTruthy();
    expect(
      remount.queryByRole("button", { name: /提交问卷|Submit Survey/i }),
    ).toBeNull();
  });

  it("reuses the exact same responseId when retrying after a transient network error", async () => {
    const seenResponseIds: string[] = [];
    let attempt = 0;
    const fetchMock: typeof fetch = vi.fn(async (_input, init) => {
      attempt += 1;
      const body = JSON.parse(String(init?.body ?? "{}")) as {
        responseId: string;
      };
      seenResponseIds.push(body.responseId);
      if (attempt === 1) {
        return jsonResponse({ detail: "Temporary server issue" }, 503);
      }
      return jsonResponse({ status: "ok", id: 303, deduplicated: false });
    });

    let counter = 0;
    const { container, getByRole, getByText } = render(
      <App
        locationObj={createFakeLocation("?type=pre&source=unity&studySessionId=sid-retry")}
        fetchImpl={fetchMock}
        storage={createMemoryStorage()}
        idGenerator={() => `stable-id-${++counter}`}
      />,
    );

    const fieldsets = Array.from(container.querySelectorAll("fieldset"));
    fireEvent.input(within(fieldsets[0]).getByRole("textbox"), {
      target: { value: "G02-A" },
    });
    fireEvent.click(within(fieldsets[1]).getByRole("radio", { name: /男/ }));
    fireEvent.click(within(fieldsets[2]).getByRole("radio", { name: /25–34岁/ }));
    fireEvent.click(within(fieldsets[3]).getByRole("radio", { name: /是/ }));
    fireEvent.click(within(fieldsets[4]).getByRole("radio", { name: /偶尔使用/ }));
    fireEvent.click(within(fieldsets[5]).getByRole("radio", { name: /是/ }));

    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(getByText(/Temporary server issue/)).toBeTruthy();
    });

    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(getByText(/问卷已提交/)).toBeTruthy();
    });

    expect(seenResponseIds).toEqual(["stable-id-1", "stable-id-1"]);
  });

  it("supports declarative textarea with maxLength counter and showWhen conditional visibility (skipping hidden questions in progress, validation, and payload)", async () => {
    const customSurvey: SurveyDefinition = {
      id: "custom_branching_survey",
      routeKey: "custom",
      version: "v2.0-test",
      title: { zh: "条件分支测试问卷", en: "Conditional Branching Test" },
      description: { zh: "测试文本题与条件显示。", en: "Tests textarea and showWhen." },
      sections: [
        {
          id: "branch_sec",
          title: { zh: "分支部分", en: "Branching Section" },
          questions: [
            {
              id: "used_hint",
              type: "single_choice",
              required: true,
              prompt: { zh: "你使用了提示吗？", en: "Did you use a hint?" },
              options: [
                { id: "yes", index: 1, label: { zh: "是", en: "Yes" } },
                { id: "no", index: 2, label: { zh: "否", en: "No" } },
              ],
            },
            {
              id: "hint_feedback",
              type: "textarea",
              required: true,
              maxLength: 120,
              prompt: { zh: "请描述提示的帮助", en: "Describe how the hint helped" },
              placeholder: { zh: "输入想法...", en: "Write your thoughts..." },
              showWhen: {
                type: "equals",
                questionId: "used_hint",
                value: "yes",
              },
            },
          ],
        },
      ],
    };

    const requests: Array<Record<string, unknown>> = [];
    const fetchMock: typeof fetch = vi.fn(async (_input, init) => {
      requests.push(
        JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      );
      return jsonResponse({ status: "ok", id: 404 });
    });

    const { container, findByRole, getByRole, getByText, queryByRole } = render(
      <App
        surveyOverride={customSurvey}
        locationObj={createFakeLocation("")}
        fetchImpl={fetchMock}
        storage={createMemoryStorage()}
      />,
    );

    const getProgressText = () =>
      container.querySelector(".progress-count")?.textContent?.trim() ?? "";

    // Initially only 1 visible question (hint_feedback hidden by showWhen)
    expect(container.querySelectorAll("fieldset")).toHaveLength(1);
    expect(getProgressText()).toBe("0 / 1");

    // Select "Yes" -> textarea becomes visible and progress total updates to 2
    fireEvent.click(getByRole("radio", { name: /是/ }));
    expect(container.querySelectorAll("fieldset")).toHaveLength(2);
    expect(getProgressText()).toBe("1 / 2");
    expect(getByText("0 / 120")).toBeTruthy();

    // Submitting without filling required visible textarea fails validation
    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    expect(fetchMock).not.toHaveBeenCalled();

    // Type into textarea -> live counter updates
    const textarea = getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.input(textarea, { target: { value: "Helpful hint!" } });
    expect(getByText("13 / 120")).toBeTruthy();
    expect(getProgressText()).toBe("2 / 2");

    // Switch back to "No" -> textarea hidden again, skipped in validation & omitted from submitted answers
    fireEvent.click(getByRole("radio", { name: /否/ }));
    expect(queryByRole("textbox")).toBeNull();
    expect(getProgressText()).toBe("1 / 1");

    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(requests).toHaveLength(1);
    });

    const submittedAnswers = requests[0].answers as Array<Record<string, unknown>>;
    expect(submittedAnswers).toHaveLength(1);
    expect(submittedAnswers[0]).toMatchObject({
      questionId: "used_hint",
      optionId: "no",
    });

    // Reset in standalone mode, choose "Yes" + textarea, and verify textarea payload
    const resetBtn = await findByRole("button", {
      name: /再填一份测试问卷|Fill Another Test Response/i,
    });
    fireEvent.click(resetBtn);
    fireEvent.click(getByRole("radio", { name: /是/ }));
    fireEvent.input(getByRole("textbox"), {
      target: { value: "  Clear guidance on box order  " },
    });
    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(requests).toHaveLength(2);
    });

    const secondAnswers = requests[1].answers as Array<Record<string, unknown>>;
    expect(secondAnswers).toHaveLength(2);
    expect(secondAnswers[1]).toMatchObject({
      sectionId: "branch_sec",
      questionIndex: 2,
      questionId: "hint_feedback",
      questionType: "textarea",
      optionId: null,
      optionIndex: null,
      score: null,
      textValue: "Clear guidance on box order",
    });
  });

  it("handles restricted localStorage gracefully and triggers Close Tab fallback hint", async () => {
    const throwingStorage: Storage = {
      length: 0,
      clear() {
        throw new Error("SecurityError");
      },
      getItem() {
        throw new Error("SecurityError");
      },
      key() {
        return null;
      },
      removeItem() {
        throw new Error("SecurityError");
      },
      setItem() {
        throw new Error("SecurityError");
      },
    };
    const closeSpy = vi.fn();
    const fetchMock: typeof fetch = vi.fn(async () =>
      jsonResponse({ status: "ok", id: 505, deduplicated: false }),
    );

    const { container, getByRole, getByText } = render(
      <App
        locationObj={createFakeLocation("?type=pre&source=unity&studySessionId=sid-locked")}
        windowObj={{ close: closeSpy }}
        fetchImpl={fetchMock}
        storage={throwingStorage}
      />,
    );

    const fieldsets = Array.from(container.querySelectorAll("fieldset"));
    fireEvent.input(within(fieldsets[0]).getByRole("textbox"), {
      target: { value: "G03-A" },
    });
    fireEvent.click(within(fieldsets[1]).getByRole("radio", { name: /男/ }));
    fireEvent.click(within(fieldsets[2]).getByRole("radio", { name: /35岁及以上/ }));
    fireEvent.click(within(fieldsets[3]).getByRole("radio", { name: /否/ }));
    fireEvent.click(within(fieldsets[4]).getByRole("radio", { name: /几乎每天使用/ }));
    fireEvent.click(within(fieldsets[5]).getByRole("radio", { name: /是/ }));

    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(getByText(/问卷已提交/)).toBeTruthy();
    });

    fireEvent.click(getByRole("button", { name: /关闭此页面|Close Tab/i }));
    expect(closeSpy).toHaveBeenCalledTimes(1);
    expect(getByText(/如果浏览器未自动关闭标签页/)).toBeTruthy();
  });

  it("keeps likert_5 sliders strictly unanswered initially and blocks submission with validation errors", () => {
    const fetchMock: typeof fetch = vi.fn(async () =>
      jsonResponse({ status: "ok", id: 601, deduplicated: false }),
    );

    const { container, getByRole } = render(
      <App
        locationObj={createFakeLocation("?type=post")}
        fetchImpl={fetchMock}
        storage={createMemoryStorage()}
      />,
    );

    const fieldsets = Array.from(container.querySelectorAll("fieldset"));
    expect(fieldsets).toHaveLength(20);

    const firstLikert = fieldsets[3];
    const sliderWrap = firstLikert.querySelector(".likert-slider") as HTMLElement;
    const sliderInput = within(firstLikert).getByRole("slider") as HTMLInputElement;
    const fillBar = firstLikert.querySelector(".likert-track-fill") as HTMLElement;

    expect(sliderWrap.getAttribute("data-answered")).toBe("false");
    expect(sliderInput.getAttribute("aria-valuenow")).toBeNull();
    expect(fillBar.style.width).toBe("0%");
    expect(
      within(firstLikert).getByText(
        "请拖动滑块或点击刻度评分 / Drag slider or click a tick to rate",
      ),
    ).toBeTruthy();

    // Language toggle updates inactive prompt copy
    fireEvent.click(getByRole("button", { name: "中文" }));
    expect(
      within(firstLikert).getByText("请拖动滑块或点击刻度评分"),
    ).toBeTruthy();
    fireEvent.click(getByRole("button", { name: "EN" }));
    expect(
      within(firstLikert).getByText("Drag slider or click a tick to rate"),
    ).toBeTruthy();
    fireEvent.click(getByRole("button", { name: "双语" }));

    // Submitting without interacting must NOT treat midpoint 3 as answered (18 visible required questions)
    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelectorAll(".question-card--error")).toHaveLength(18);
    expect(container.querySelector(".progress-count")?.textContent?.trim()).toBe(
      "0 / 20",
    );
  });

  it("activates likert_5 slider on drag/change, midpoint 3 track click, tick 3 click, and keyboard input while preserving payload", async () => {
    const requests: Array<Record<string, unknown>> = [];
    const fetchMock: typeof fetch = vi.fn(async (_input, init) => {
      requests.push(
        JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      );
      return jsonResponse({ status: "ok", id: 602, deduplicated: false });
    });

    const likertOnlySurvey: SurveyDefinition = {
      id: "likert_slider_test",
      routeKey: "likert-test",
      version: "v1.0-test",
      title: { zh: "滑块测试问卷", en: "Slider Test Survey" },
      description: { zh: "验证量表滑块交互。", en: "Verify likert slider interactions." },
      sections: [
        {
          id: "sec_slider",
          title: { zh: "量表题", en: "Likert Items" },
          questions: ONLINE_POST_MATCH_SURVEY.sections[2].questions, // 7 likert_5 items in Part C
        },
      ],
    };

    const { container, getByRole } = render(
      <App
        surveyOverride={likertOnlySurvey}
        locationObj={createFakeLocation("")}
        fetchImpl={fetchMock}
        storage={createMemoryStorage()}
      />,
    );

    const fieldsets = Array.from(container.querySelectorAll("fieldset"));
    expect(fieldsets).toHaveLength(7);

    // 1. Drag / input to score 4
    const slider0 = within(fieldsets[0]).getByRole("slider") as HTMLInputElement;
    fireEvent.input(slider0, { target: { value: "4" } });
    expect(
      fieldsets[0].querySelector(".likert-slider")?.getAttribute("data-answered"),
    ).toBe("true");
    expect(slider0.getAttribute("aria-valuenow")).toBe("4");
    expect(
      (fieldsets[0].querySelector(".likert-track-fill") as HTMLElement).style
        .width,
    ).toBe("75%");
    expect(within(fieldsets[0]).getByText("4 · 同意 / Agree")).toBeTruthy();

    // 2. Midpoint 3 activation via direct range track click while unanswered
    const slider1 = within(fieldsets[1]).getByRole("slider") as HTMLInputElement;
    expect(
      fieldsets[1].querySelector(".likert-slider")?.getAttribute("data-answered"),
    ).toBe("false");
    fireEvent.click(slider1);
    expect(
      fieldsets[1].querySelector(".likert-slider")?.getAttribute("data-answered"),
    ).toBe("true");
    expect(slider1.getAttribute("aria-valuenow")).toBe("3");
    expect(
      (fieldsets[1].querySelector(".likert-track-fill") as HTMLElement).style
        .width,
    ).toBe("50%");
    expect(
      within(fieldsets[1]).getByText("3 · 一般 / Neither agree nor disagree"),
    ).toBeTruthy();

    // 3. Midpoint 3 activation via tick 3 button click while unanswered
    const tick3Btn = fieldsets[2].querySelector(
      'button[data-score="3"]',
    ) as HTMLButtonElement;
    expect(tick3Btn).toBeTruthy();
    fireEvent.click(tick3Btn);
    expect(
      fieldsets[2].querySelector(".likert-slider")?.getAttribute("data-answered"),
    ).toBe("true");
    expect(tick3Btn.getAttribute("aria-pressed")).toBe("true");
    expect(
      within(fieldsets[2]).getByText("3 · 一般 / Neither agree nor disagree"),
    ).toBeTruthy();

    // 4-7. Keyboard activation on unanswered sliders
    const slider3 = within(fieldsets[3]).getByRole("slider");
    fireEvent.keyDown(slider3, { key: "ArrowLeft" });
    expect(slider3.getAttribute("aria-valuenow")).toBe("2");
    expect(within(fieldsets[3]).getByText("2 · 不同意 / Disagree")).toBeTruthy();

    const slider4 = within(fieldsets[4]).getByRole("slider");
    fireEvent.keyDown(slider4, { key: "ArrowRight" });
    expect(slider4.getAttribute("aria-valuenow")).toBe("4");

    const slider5 = within(fieldsets[5]).getByRole("slider");
    fireEvent.keyDown(slider5, { key: "Home" });
    expect(slider5.getAttribute("aria-valuenow")).toBe("1");
    expect(
      (fieldsets[5].querySelector(".likert-track-fill") as HTMLElement).style
        .width,
    ).toBe("0%");

    const slider6 = within(fieldsets[6]).getByRole("slider");
    fireEvent.keyDown(slider6, { key: " " });
    expect(slider6.getAttribute("aria-valuenow")).toBe("3");

    expect(container.querySelector(".progress-count")?.textContent?.trim()).toBe(
      "7 / 7",
    );

    // Submit and verify exact answer payload contract
    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(requests).toHaveLength(1);
    });

    const submittedAnswers = requests[0].answers as Array<
      Record<string, unknown>
    >;
    expect(submittedAnswers).toHaveLength(7);
    expect(submittedAnswers[0]).toMatchObject({
      optionId: "4",
      optionIndex: 4,
      score: 4,
      optionTextZh: "同意",
      optionTextEn: "Agree",
    });
    expect(submittedAnswers[1]).toMatchObject({
      optionId: "3",
      optionIndex: 3,
      score: 3,
      optionTextZh: "一般",
      optionTextEn: "Neither agree nor disagree",
    });
    expect(submittedAnswers[3]).toMatchObject({
      optionId: "2",
      optionIndex: 2,
      score: 2,
      optionTextZh: "不同意",
      optionTextEn: "Disagree",
    });
    expect(submittedAnswers[5]).toMatchObject({
      optionId: "1",
      optionIndex: 1,
      score: 1,
      optionTextZh: "非常不同意",
      optionTextEn: "Strongly disagree",
    });
  });

  it("buildSurveyUrlWithType falls back to globalThis.location and preserves existing query params", () => {
    const originalUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    try {
      window.history.replaceState(
        {},
        "",
        "/survey/?lang=en&participantId=P01",
      );
      expect(buildSurveyUrlWithType("pre")).toBe(
        "/survey/?lang=en&participantId=P01&type=pre",
      );

      // Replaces prior type and removes legacy aliases while keeping other params
      window.history.replaceState(
        {},
        "",
        "/survey/?lang=zh&type=post&survey=post&surveyId=online_post_match_survey&participantId=P02",
      );
      expect(buildSurveyUrlWithType("pre")).toBe(
        "/survey/?lang=zh&type=pre&participantId=P02",
      );

      // Removing type preserves remaining query parameters
      expect(buildSurveyUrlWithType(null)).toBe(
        "/survey/?lang=zh&participantId=P02",
      );

      // Landing card click without explicit locationObj preserves globalThis.location params
      window.history.replaceState(
        {},
        "",
        "/survey/?lang=en&participantId=P01",
      );
      const pushState = vi.fn();
      const fakeWindow = {
        history: { pushState, replaceState: vi.fn() },
        close: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      };
      const { getByRole } = render(
        <App windowObj={fakeWindow} storage={createMemoryStorage()} />,
      );
      fireEvent.click(
        getByRole("button", {
          name: /匹配前简短问卷|Pre-Match Short Questionnaire/i,
        }),
      );
      expect(pushState).toHaveBeenCalledWith(
        { surveyId: "online_pre_match_survey" },
        "",
        "/survey/?lang=en&participantId=P01&type=pre",
      );
    } finally {
      window.history.replaceState({}, "", originalUrl || "/");
    }
  });

  it("prefers URL participantId over form participant_code while keeping participant_code answer payload", async () => {
    const requests: Array<Record<string, unknown>> = [];
    const fetchMock: typeof fetch = vi.fn(async (_input, init) => {
      requests.push(
        JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      );
      return jsonResponse({ status: "ok", id: 701, deduplicated: false });
    });

    const { container, getByRole } = render(
      <App
        locationObj={createFakeLocation("?type=pre&participantId=URL-P99")}
        fetchImpl={fetchMock}
        storage={createMemoryStorage()}
      />,
    );

    const fieldsets = Array.from(container.querySelectorAll("fieldset"));
    fireEvent.input(within(fieldsets[0]).getByRole("textbox"), {
      target: { value: "G02-A" },
    });
    fireEvent.click(within(fieldsets[1]).getByRole("radio", { name: /男/ }));
    fireEvent.click(within(fieldsets[2]).getByRole("radio", { name: /18–24岁/ }));
    fireEvent.click(within(fieldsets[3]).getByRole("radio", { name: /是/ }));
    fireEvent.click(within(fieldsets[4]).getByRole("radio", { name: /从未用过/ }));
    fireEvent.click(within(fieldsets[5]).getByRole("radio", { name: /是/ }));

    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(requests).toHaveLength(1);
    });

    expect(requests[0].participantId).toBe("URL-P99");
    const submittedAnswers = requests[0].answers as Array<Record<string, unknown>>;
    expect(submittedAnswers[0]).toMatchObject({
      questionId: "participant_code",
      questionType: "textarea",
      textValue: "G02-A",
    });
  });

  it("hides Part A relationship Likert items for stranger (even after reverting from friend) and submits optional Part D reflections only when filled", async () => {
    const requests: Array<Record<string, unknown>> = [];
    const fetchMock: typeof fetch = vi.fn(async (_input, init) => {
      requests.push(
        JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      );
      return jsonResponse({ status: "ok", id: 702, deduplicated: false });
    });

    const { container, getByRole } = render(
      <App
        locationObj={createFakeLocation("?type=post")}
        fetchImpl={fetchMock}
        storage={createMemoryStorage()}
      />,
    );

    const getRelCard = (id: string) =>
      container.querySelector(`[data-question-id="${id}"]`);

    // Initially unselected -> all 6 rel_* items are hidden
    expect(getRelCard("rel_comfort")).toBeNull();
    expect(getRelCard("rel_support_dismissive")).toBeNull();

    // Selecting stranger keeps them hidden
    const relCard = container.querySelector(
      '[data-question-id="opponent_relationship"]',
    ) as HTMLElement;
    fireEvent.click(within(relCard).getByRole("radio", { name: /陌生人/ }));
    expect(getRelCard("rel_comfort")).toBeNull();

    // Switching to friend reveals all 6; fill rel_comfort, then revert to stranger
    fireEvent.click(within(relCard).getByRole("radio", { name: /朋友/ }));
    const comfortCard = getRelCard("rel_comfort") as HTMLElement;
    expect(comfortCard).toBeTruthy();
    expect(getRelCard("rel_support_dismissive")).toBeTruthy();
    fireEvent.input(within(comfortCard).getByRole("slider"), {
      target: { value: "5" },
    });

    fireEvent.click(within(relCard).getByRole("radio", { name: /陌生人/ }));
    expect(getRelCard("rel_comfort")).toBeNull();
    expect(getRelCard("rel_support_dismissive")).toBeNull();

    // Fill participant_code, round_number, all remaining visible sliders, AND both optional Part D textareas
    const codeCard = container.querySelector(
      '[data-question-id="participant_code"]',
    ) as HTMLElement;
    fireEvent.input(within(codeCard).getByRole("textbox"), {
      target: { value: "G02-B" },
    });
    const roundCard = container.querySelector(
      '[data-question-id="round_number"]',
    ) as HTMLElement;
    fireEvent.click(within(roundCard).getByRole("radio", { name: /第 2 轮/ }));

    for (const fs of Array.from(container.querySelectorAll("fieldset"))) {
      const slider = within(fs).queryByRole("slider") as HTMLInputElement | null;
      if (slider) {
        fireEvent.input(slider, { target: { value: "3" } });
      }
    }

    const intentCard = container.querySelector(
      '[data-question-id="final_self_reported_intent"]',
    ) as HTMLElement;
    const reflectionCard = container.querySelector(
      '[data-question-id="ai_collaboration_reflection"]',
    ) as HTMLElement;
    fireEvent.input(within(intentCard).getByRole("textbox"), {
      target: { value: "  Keep the center detour tight.  " },
    });
    fireEvent.input(within(reflectionCard).getByRole("textbox"), {
      target: { value: "  AI caught a deadlock risk.  " },
    });

    fireEvent.click(getByRole("button", { name: /提交问卷|Submit Survey/i }));
    await waitFor(() => {
      expect(requests).toHaveLength(1);
    });

    const submittedAnswers = requests[0].answers as Array<Record<string, unknown>>;
    // 20 visible questions total under stranger condition (26 - 6 rel_*); none of rel_* included
    expect(submittedAnswers).toHaveLength(20);
    expect(
      submittedAnswers.some((a) => String(a.questionId).startsWith("rel_")),
    ).toBe(false);
    expect(submittedAnswers[18]).toMatchObject({
      sectionId: "part_d",
      questionIndex: 25,
      questionId: "final_self_reported_intent",
      questionType: "textarea",
      optionId: null,
      score: null,
      textValue: "Keep the center detour tight.",
    });
    expect(submittedAnswers[19]).toMatchObject({
      sectionId: "part_d",
      questionIndex: 26,
      questionId: "ai_collaboration_reflection",
      questionType: "textarea",
      optionId: null,
      score: null,
      textValue: "AI caught a deadlock risk.",
    });
  });
});
