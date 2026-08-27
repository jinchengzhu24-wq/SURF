using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

// The DG scene owns every visible control. This controller binds only serialized references.
public sealed class DescriptionGenerationController : MonoBehaviour
{
    private const string BackendBaseUrl = "http://111.231.136.4:8000";
    private const string GuideEndpoint = BackendBaseUrl + "/dg/guide/summary";
    private static readonly string[] DifficultyLabels = { "Easy", "Medium", "Hard", "Random" };
    private static readonly string[] LayoutLabels = { "Compact", "Balanced", "Open", "Random" };
    private static readonly DifficultyPreset[] DifficultyPresets =
    {
        new DifficultyPreset("Easy", 18, 32, 8, 14, 14, 24),
        new DifficultyPreset("Medium", 22, 42, 10, 22, 18, 34),
        new DifficultyPreset("Hard", 30, 50, 16, 28, 24, 40)
    };
    private static readonly LayoutPreset[] LayoutPresets =
    {
        new LayoutPreset("Compact", "bottleneck_corridor", "edge_cluster", "side_choke", "side_pool", "center", 1, "vertical", "player_route", "required"),
        new LayoutPreset("Balanced", "split_route", "split_pair", "central_baffle", "route_divider", "none", 0, "any", "visual_only", "preferred"),
        new LayoutPreset("Open", "open_workshop", "split_pair", "central_baffle", "side_pool", "none", 0, "any", "visual_only", "preferred")
    };
    private static readonly Question[] Questions =
    {
        new Question("How much inspection should the map require before the first move?", new[] { "Little inspection before acting", "Some inspection of boxes, goals, and passages", "Broader inspection and planning before acting", "No preference" }, new[] { "quick_start", "observe_then_decide", "plan_ahead", "no_preference" }),
        new Question("How much should box-push decisions depend on other pushes?", new[] { "Most pushes can be considered independently", "Some pushes depend on position or order", "Several pushes depend on one another", "No preference" }, new[] { "easy_to_adjust", "consider_order", "connected_pushes", "no_preference" }),
        new Question("How should important positions be distributed across the playable space?", new[] { "Concentrated within one main area", "Distributed across a few connected areas", "Distributed across a wider area", "No preference" }, new[] { "focused_area", "connected_areas", "wide_area", "no_preference" }),
        new Question("What route structure would you prefer?", new[] { "Short routes connecting nearby decisions", "Mostly direct routes with some detours", "Longer routes with exploration or return paths", "No preference" }, new[] { "short_routes", "occasional_detours", "long_routes", "no_preference" })
    };

    [Header("Navigation")]
    public string nextSceneName = "DG_Level";
    [Header("Request")]
    [SerializeField] private bool restoreSavedSettings;
    [SerializeField] private string guideEndpoint = GuideEndpoint;
    [SerializeField] private string backendBaseUrl = BackendBaseUrl;
    [SerializeField] private int requestTimeoutSeconds = 30;
    [Header("Static Scene References")]
    [SerializeField] private Text progressText;
    [SerializeField] private Text summaryText;
    [SerializeField] private Text difficultyRationaleText;
    [SerializeField] private Text layoutRationaleText;
    [SerializeField] private Text statusText;
    [SerializeField] private Text difficultyText;
    [SerializeField] private Text layoutText;
    [SerializeField] private GameObject difficultyControls;
    [SerializeField] private Text questionText;
    [SerializeField] private Button[] optionButtons;
    [SerializeField] private Button previousLayoutButton;
    [SerializeField] private Button nextLayoutButton;
    [SerializeField] private Button previousDifficultyButton;
    [SerializeField] private Button nextDifficultyButton;
    [SerializeField] private Button confirmButton;

    private DescriptionGenerationSettings settings;
    private int displayedDifficulty;
    private int displayedLayout;
    private int questionIndex;
    private bool summaryReady;
    private bool requestInFlight;

    private sealed class Question
    {
        public readonly string prompt;
        public readonly string[] labels;
        public readonly string[] values;

        public Question(string prompt, string[] labels, string[] values)
        {
            this.prompt = prompt;
            this.labels = labels;
            this.values = values;
        }
    }

    private sealed class DifficultyPreset
    {
        public readonly string label;
        private readonly int minSteps, maxSteps, minPushes, maxPushes, minPulls, maxPulls;

        public DifficultyPreset(string label, int minSteps, int maxSteps, int minPushes, int maxPushes, int minPulls, int maxPulls)
        {
            this.label = label;
            this.minSteps = minSteps;
            this.maxSteps = maxSteps;
            this.minPushes = minPushes;
            this.maxPushes = maxPushes;
            this.minPulls = minPulls;
            this.maxPulls = maxPulls;
        }

        public void Apply(LevelGenerationPreferences target)
        {
            target.minSolutionSteps = minSteps;
            target.maxSolutionSteps = maxSteps;
            target.minPushes = minPushes;
            target.maxPushes = maxPushes;
            target.minReversePulls = minPulls;
            target.maxReversePulls = maxPulls;
        }

        public bool Matches(LevelGenerationPreferences value)
        {
            return value.minSolutionSteps == minSteps && value.maxSolutionSteps == maxSteps
                && value.minPushes == minPushes && value.maxPushes == maxPushes
                && value.minReversePulls == minPulls && value.maxReversePulls == maxPulls;
        }
    }

    private sealed class LayoutPreset
    {
        public readonly string label;
        private readonly string archetype, targetLayout, obstacleStyle, waterStyle;
        private readonly string corridorPlacement, corridorOrientation, corridorRole, corridorPriority;
        private readonly int corridorWidth;

        public LayoutPreset(string label, string archetype, string targetLayout, string obstacleStyle, string waterStyle, string corridorPlacement, int corridorWidth, string corridorOrientation, string corridorRole, string corridorPriority)
        {
            this.label = label;
            this.archetype = archetype;
            this.targetLayout = targetLayout;
            this.obstacleStyle = obstacleStyle;
            this.waterStyle = waterStyle;
            this.corridorPlacement = corridorPlacement;
            this.corridorWidth = corridorWidth;
            this.corridorOrientation = corridorOrientation;
            this.corridorRole = corridorRole;
            this.corridorPriority = corridorPriority;
        }

        public void Apply(LevelGenerationPreferences target)
        {
            target.archetype = archetype;
            target.targetLayout = targetLayout;
            target.obstacleStyle = obstacleStyle;
            target.waterStyle = waterStyle;
            target.corridorPlacement = corridorPlacement;
            target.corridorWidth = corridorWidth;
            target.corridorOrientation = corridorOrientation;
            target.corridorRole = corridorRole;
            target.corridorPriority = corridorPriority;
        }

        public bool Matches(LevelGenerationPreferences value)
        {
            return value.archetype == archetype && value.targetLayout == targetLayout
                && value.obstacleStyle == obstacleStyle && value.waterStyle == waterStyle
                && value.corridorPlacement == corridorPlacement && value.corridorWidth == corridorWidth
                && value.corridorOrientation == corridorOrientation && value.corridorRole == corridorRole
                && value.corridorPriority == corridorPriority;
        }
    }

    private void Start()
    {
        if (!HasValidStaticInterface())
        {
            Debug.LogError("DescriptionGenerationController: assign every static DG scene reference in the Inspector.");
            enabled = false;
            return;
        }

        DescriptionGenerationSettings saved = null;
        bool loaded = restoreSavedSettings && DescriptionGenerationContext.TryLoad(out saved);
        settings = loaded && saved != null ? saved : NewSettings();
        displayedDifficulty = loaded ? ResolveSavedDifficulty() : 1;
        displayedLayout = loaded ? ResolveSavedLayout() : 1;
        BindStaticControls();
        ShowQuestion(0);
    }

    private bool HasValidStaticInterface()
    {
        return questionText != null && summaryText != null && difficultyRationaleText != null && layoutRationaleText != null
            && difficultyText != null && layoutText != null && difficultyControls != null
            && previousLayoutButton != null && nextLayoutButton != null && previousDifficultyButton != null
            && nextDifficultyButton != null && confirmButton != null && optionButtons != null && optionButtons.Length == 4
            && optionButtons[0] != null && optionButtons[1] != null && optionButtons[2] != null && optionButtons[3] != null;
    }

    private void BindStaticControls()
    {
        previousDifficultyButton.onClick.RemoveAllListeners();
        previousDifficultyButton.onClick.AddListener(() => ChangeDifficulty(-1));
        nextDifficultyButton.onClick.RemoveAllListeners();
        nextDifficultyButton.onClick.AddListener(() => ChangeDifficulty(1));
        previousLayoutButton.onClick.RemoveAllListeners();
        previousLayoutButton.onClick.AddListener(() => ChangeLayout(-1));
        nextLayoutButton.onClick.RemoveAllListeners();
        nextLayoutButton.onClick.AddListener(() => ChangeLayout(1));
        confirmButton.onClick.RemoveAllListeners();
        confirmButton.onClick.AddListener(SaveAndContinue);
        UpdateParameterUi();
        SetControlsInteractable(false);
    }

    private void ShowQuestion(int index)
    {
        questionIndex = index;
        Question question = Questions[index];
        if (progressText != null) progressText.text = "Question " + (index + 1) + " of " + Questions.Length;
        questionText.text = question.prompt;
        if (statusText != null) statusText.text = "Choose one option.";
        for (int i = 0; i < optionButtons.Length; i++)
        {
            int answerIndex = i;
            SetButtonLabel(optionButtons[i], question.labels[i]);
            optionButtons[i].onClick.RemoveAllListeners();
            optionButtons[i].onClick.AddListener(() => SelectAnswer(answerIndex));
            optionButtons[i].interactable = true;
        }
        SetControlsInteractable(false);
    }

    private void SelectAnswer(int index)
    {
        string value = Questions[questionIndex].values[index];
        if (questionIndex == 0) settings.firstMovePreference = value;
        else if (questionIndex == 1) settings.pushPlanningPreference = value;
        else if (questionIndex == 2) settings.spacePreference = value;
        else settings.routeRhythmPreference = value;

        if (questionIndex + 1 < Questions.Length)
        {
            ShowQuestion(questionIndex + 1);
            return;
        }
        StartCoroutine(RequestSummary());
    }

    private IEnumerator RequestSummary()
    {
        requestInFlight = true;
        SetOptionButtonsInteractable(false);
        questionText.text = "AI is reviewing your initial-map preferences.";
        summaryText.text = "AI reflection is being prepared...";
        difficultyRationaleText.text = "AI difficulty suggestion is being prepared...";
        layoutRationaleText.text = "AI layout suggestion is being prepared...";
        if (statusText != null) statusText.text = "AI is preparing difficulty and layout suggestions...";
        GuideRequest payload = new GuideRequest
        {
            firstMovePreference = settings.firstMovePreference,
            pushPlanningPreference = settings.pushPlanningPreference,
            spacePreference = settings.spacePreference,
            routeRhythmPreference = settings.routeRhythmPreference,
            language = "en"
        };
        string endpoint = string.IsNullOrWhiteSpace(guideEndpoint) ? GuideEndpoint : guideEndpoint.Trim();
        using (UnityWebRequest request = new UnityWebRequest(endpoint, UnityWebRequest.kHttpVerbPOST))
        {
            request.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(JsonUtility.ToJson(payload)));
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            yield return request.SendWebRequest();
            if (request.result == UnityWebRequest.Result.Success)
            {
                GuideResponse response = null;
                try { response = JsonUtility.FromJson<GuideResponse>(request.downloadHandler.text); }
                catch (Exception exception) { Debug.LogWarning("DG guide response parse failed: " + exception.Message); }
                if (IsValidGuideResponse(response))
                {
                    requestInFlight = false;
                    ApplyGuideResponse(response);
                    yield break;
                }
            }
        }
        requestInFlight = false;
        ApplyGuideResponse(BuildFallbackResponse());
    }

    private bool IsValidGuideResponse(GuideResponse response)
    {
        return response != null && IsValidDifficulty(response.recommendedDifficulty) && IsValidLayout(response.recommendedLayout)
            && !string.IsNullOrWhiteSpace(response.summary) && !string.IsNullOrWhiteSpace(response.difficultyRationale)
            && !string.IsNullOrWhiteSpace(response.layoutRationale)
            && IsAllowedGuideRecommendation(response);
    }

    private GuideResponse BuildFallbackResponse()
    {
        int difficultyScore = ResolvePreferenceScore(settings.firstMovePreference, settings.pushPlanningPreference);
        int layoutScore = ResolvePreferenceScore(settings.spacePreference, settings.routeRhythmPreference);
        string difficulty = DifficultyLabels[difficultyScore];
        string layout = LayoutLabels[layoutScore];
        return new GuideResponse
        {
            summary = BuildFallbackIntentSummary(),
            recommendedDifficulty = difficulty,
            difficultyRationale = BuildDifficultyFallbackRationale(difficulty),
            recommendedLayout = layout,
            layoutRationale = BuildLayoutFallbackRationale(layout),
            source = "deterministic_fallback"
        };
    }

    private string BuildFallbackIntentSummary()
    {
        string firstMove = FirstMovePhrase(settings.firstMovePreference);
        string pushPlanning = PushPlanningPhrase(settings.pushPlanningPreference);
        string space = SpacePhrase(settings.spacePreference);
        string route = RoutePhrase(settings.routeRhythmPreference);
        string summary = (
            "Your choices suggest a level where the first move can " + firstMove + " and pushes can " + pushPlanning
            + ". For the space, the choices point toward " + space + " with routes that " + route + "."
        );
        return summary.Length > 480 ? summary.Substring(0, 480).TrimEnd() : summary;
    }

    private static string FirstMovePhrase(string value)
    {
        if (value == "quick_start") return "get moving with little inspection";
        if (value == "observe_then_decide") return "look over boxes, goals, and passages before settling on a move";
        if (value == "plan_ahead") return "inspect the map broadly and plan before acting";
        return "leave the first move open-ended";
    }

    private static string PushPlanningPhrase(string value)
    {
        if (value == "easy_to_adjust") return "treat most pushes as independent decisions";
        if (value == "consider_order") return "watch how position and order affect some pushes";
        if (value == "connected_pushes") return "connect several pushes into one fuller plan";
        return "leave push dependencies open-ended";
    }

    private static string SpacePhrase(string value)
    {
        if (value == "focused_area") return "keep important positions in one focused area";
        if (value == "connected_areas") return "spread them across a few connected areas";
        if (value == "wide_area") return "give them room across a wider playable area";
        return "leave the position distribution open-ended";
    }

    private static string RoutePhrase(string value)
    {
        if (value == "short_routes") return "use short routes between nearby decisions";
        if (value == "occasional_detours") return "keep progress mostly direct with occasional detours";
        if (value == "long_routes") return "include longer routes with exploration or returns";
        return "leave the route rhythm open-ended";
    }

    private static int ResolvePreferenceScore(string first, string second)
    {
        int firstScore = PreferenceScore(first);
        int secondScore = PreferenceScore(second);
        if (firstScore < 0 && secondScore < 0) return DifficultyLabels.Length - 1;
        if (firstScore < 0) return secondScore;
        if (secondScore < 0) return firstScore;
        int low = Mathf.Min(firstScore, secondScore);
        int high = Mathf.Max(firstScore, secondScore);
        if (high - low == 1) return low == 0 ? low : high;
        return 1;
    }

    private static int PreferenceScore(string value)
    {
        if (value == "quick_start" || value == "easy_to_adjust" || value == "focused_area" || value == "short_routes") return 0;
        if (value == "observe_then_decide" || value == "consider_order" || value == "connected_areas" || value == "occasional_detours") return 1;
        if (value == "plan_ahead" || value == "connected_pushes" || value == "wide_area" || value == "long_routes") return 2;
        return -1;
    }

    private bool IsAllowedGuideRecommendation(GuideResponse response)
    {
        int difficultyBaseline = ResolvePreferenceScore(settings.firstMovePreference, settings.pushPlanningPreference);
        int layoutBaseline = ResolvePreferenceScore(settings.spacePreference, settings.routeRhythmPreference);
        return IsAllowedRecommendation(
            response.recommendedDifficulty,
            difficultyBaseline,
            settings.firstMovePreference,
            settings.pushPlanningPreference,
            DifficultyLabels
        ) && IsAllowedRecommendation(
            response.recommendedLayout,
            layoutBaseline,
            settings.spacePreference,
            settings.routeRhythmPreference,
            LayoutLabels
        );
    }

    private static bool IsAllowedRecommendation(
        string recommendation,
        int baseline,
        string first,
        string second,
        string[] labels
    )
    {
        int recommendationIndex = Array.IndexOf(labels, recommendation);
        if (baseline == labels.Length - 1) return recommendationIndex == labels.Length - 1;
        if (recommendationIndex < 0 || recommendationIndex == labels.Length - 1) return false;
        if (!HasExplicitConflict(first, second)) return recommendationIndex == baseline;
        return Mathf.Abs(recommendationIndex - baseline) <= 1;
    }

    private static bool HasExplicitConflict(string first, string second)
    {
        int firstScore = PreferenceScore(first);
        int secondScore = PreferenceScore(second);
        return firstScore >= 0 && secondScore >= 0 && firstScore != secondScore;
    }

    private string BuildDifficultyFallbackRationale(string difficulty)
    {
        if (difficulty == "Random") return "I do not see a directional planning preference in the two answers, so I would leave Difficulty Random for Confirm.";
        return "I connect the first-move preference to " + FirstMovePhrase(settings.firstMovePreference)
            + " and the push preference to " + PushPlanningPhrase(settings.pushPlanningPreference)
            + ", which supports " + difficulty + " planning complexity.";
    }

    private string BuildLayoutFallbackRationale(string layout)
    {
        if (layout == "Random") return "I do not see a directional spatial preference in the two answers, so I would leave Layout Random for Confirm.";
        return "I connect the space preference to " + SpacePhrase(settings.spacePreference)
            + " and the route preference to " + RoutePhrase(settings.routeRhythmPreference)
            + ", which supports a " + layout + " layout.";
    }

    private void ApplyGuideResponse(GuideResponse response)
    {
        settings.aiSummary = response.summary.Trim();
        settings.aiDifficultyRationale = response.difficultyRationale.Trim();
        settings.aiLayoutRationale = response.layoutRationale.Trim();
        settings.aiRecommendedDifficulty = response.recommendedDifficulty;
        settings.aiRecommendedLayout = response.recommendedLayout;
        settings.aiRecommendationSource = response.source ?? "llm";
        displayedDifficulty = DifficultyIndex(settings.aiRecommendedDifficulty);
        displayedLayout = LayoutIndex(settings.aiRecommendedLayout);
        summaryText.text = "AI reflection: " + settings.aiSummary;
        difficultyRationaleText.text = "AI difficulty suggestion: " + DifficultyLabels[displayedDifficulty] + "\n" + settings.aiDifficultyRationale;
        layoutRationaleText.text = "AI layout suggestion: " + LayoutLabels[displayedLayout] + "\n" + settings.aiLayoutRationale;
        if (statusText != null) statusText.text = "Review the suggestions, adjust either setting if needed, then confirm.";
        summaryReady = true;
        UpdateParameterUi();
        SetControlsInteractable(true);
    }

    private void ChangeDifficulty(int delta)
    {
        displayedDifficulty = (displayedDifficulty + delta + DifficultyLabels.Length) % DifficultyLabels.Length;
        UpdateParameterUi();
    }

    private void ChangeLayout(int delta)
    {
        displayedLayout = (displayedLayout + delta + LayoutLabels.Length) % LayoutLabels.Length;
        UpdateParameterUi();
    }

    private void UpdateParameterUi()
    {
        difficultyText.text = DifficultyLabels[displayedDifficulty];
        layoutText.text = LayoutLabels[displayedLayout];
    }

    private void SetControlsInteractable(bool interactable)
    {
        previousDifficultyButton.interactable = interactable;
        nextDifficultyButton.interactable = interactable;
        previousLayoutButton.interactable = interactable;
        nextLayoutButton.interactable = interactable;
        confirmButton.interactable = interactable && summaryReady && !requestInFlight;
    }

    private void SetOptionButtonsInteractable(bool interactable)
    {
        foreach (Button button in optionButtons) button.interactable = interactable;
    }

    private static DescriptionGenerationSettings NewSettings()
    {
        DescriptionGenerationSettings result = new DescriptionGenerationSettings();
        DifficultyPresets[1].Apply(result.preferences);
        LayoutPresets[1].Apply(result.preferences);
        return result;
    }

    private int ResolveSavedDifficulty()
    {
        for (int i = 0; i < DifficultyPresets.Length; i++) if (DifficultyPresets[i].Matches(settings.preferences)) return i;
        return 3;
    }

    private int ResolveSavedLayout()
    {
        for (int i = 0; i < LayoutPresets.Length; i++) if (LayoutPresets[i].Matches(settings.preferences)) return i;
        return 3;
    }

    private void SaveAndContinue()
    {
        if (!summaryReady || requestInFlight) return;
        int difficultyIndex = displayedDifficulty == DifficultyPresets.Length ? UnityEngine.Random.Range(0, DifficultyPresets.Length) : displayedDifficulty;
        int layoutIndex = displayedLayout == LayoutPresets.Length ? UnityEngine.Random.Range(0, LayoutPresets.Length) : displayedLayout;
        DifficultyPresets[difficultyIndex].Apply(settings.preferences);
        LayoutPresets[layoutIndex].Apply(settings.preferences);
        settings.finalDifficulty = DifficultyPresets[difficultyIndex].label;
        settings.finalLayout = LayoutPresets[layoutIndex].label;
        StartCoroutine(RecordDraftAndContinue());
    }

    private IEnumerator RecordDraftAndContinue()
    {
        requestInFlight = true;
        SetControlsInteractable(false);
        if (!OnlineMatchContext.HasMatch)
        {
            ShowDraftRecordFailure("No active online room is available to record Draft.");
            yield break;
        }

        string baseUrl = string.IsNullOrWhiteSpace(backendBaseUrl) ? BackendBaseUrl : backendBaseUrl.TrimEnd('/');
        string endpoint = baseUrl + "/online/rooms/" + UnityWebRequest.EscapeURL(OnlineMatchContext.MatchId) + "/draft";
        DraftRequest payload = new DraftRequest
        {
            q1Answer = settings.firstMovePreference,
            q2Answer = settings.pushPlanningPreference,
            q3Answer = settings.spacePreference,
            q4Answer = settings.routeRhythmPreference,
            aiReflection = settings.aiSummary,
            aiDifficultyRationale = settings.aiDifficultyRationale,
            aiLayoutRationale = settings.aiLayoutRationale,
            aiRecommendedDifficulty = settings.aiRecommendedDifficulty,
            aiRecommendedLayout = settings.aiRecommendedLayout,
            aiRecommendationSource = settings.aiRecommendationSource,
            finalDifficulty = settings.finalDifficulty,
            finalLayout = settings.finalLayout
        };
        using (UnityWebRequest request = new UnityWebRequest(endpoint, UnityWebRequest.kHttpVerbPOST))
        {
            request.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(JsonUtility.ToJson(payload)));
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.SetRequestHeader("X-Player-Token", OnlineMatchContext.PlayerToken);
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            if (statusText != null) statusText.text = "Recording Draft settings...";
            yield return request.SendWebRequest();
            if (request.result != UnityWebRequest.Result.Success)
            {
                ShowDraftRecordFailure(BuildErrorMessage(request));
                yield break;
            }
        }

        requestInFlight = false;
        // The neutral answers only inform this one recommendation.  The persisted
        // context (and the 8000 Draft record) contains confirmed settings only.
        settings.firstMovePreference = string.Empty;
        settings.pushPlanningPreference = string.Empty;
        settings.spacePreference = string.Empty;
        settings.routeRhythmPreference = string.Empty;
        DescriptionGenerationContext.Save(settings);
        if (!string.IsNullOrWhiteSpace(nextSceneName) && Application.CanStreamedLevelBeLoaded(nextSceneName))
        {
            SceneManager.LoadScene(nextSceneName);
            yield break;
        }
        ShowDraftRecordFailure("The DG result scene is unavailable.");
    }

    private void ShowDraftRecordFailure(string message)
    {
        requestInFlight = false;
        if (statusText != null) statusText.text = "Draft was not recorded. Retry Confirm.";
        summaryText.text = "Draft record failed: " + message;
        SetControlsInteractable(true);
    }

    private static string BuildErrorMessage(UnityWebRequest request)
    {
        string body = request.downloadHandler == null ? "" : request.downloadHandler.text;
        try
        {
            ErrorEnvelope envelope = JsonUtility.FromJson<ErrorEnvelope>(body);
            if (envelope != null && !string.IsNullOrWhiteSpace(envelope.detail)) return envelope.detail;
        }
        catch (Exception) { }
        return request.responseCode > 0 ? "Server request failed (HTTP " + request.responseCode + ")." : request.error;
    }

    private static int DifficultyIndex(string value)
    {
        for (int i = 0; i < DifficultyPresets.Length; i++) if (DifficultyPresets[i].label == value) return i;
        return -1;
    }

    private static int LayoutIndex(string value)
    {
        for (int i = 0; i < LayoutPresets.Length; i++) if (LayoutPresets[i].label == value) return i;
        return -1;
    }

    private static bool IsValidDifficulty(string value) => DifficultyIndex(value) >= 0;
    private static bool IsValidLayout(string value) => LayoutIndex(value) >= 0;

    private static void SetButtonLabel(Button button, string value)
    {
        Text label = button.GetComponentInChildren<Text>(true);
        if (label != null) label.text = value;
    }

    [Serializable] private sealed class GuideRequest { public string firstMovePreference; public string pushPlanningPreference; public string spacePreference; public string routeRhythmPreference; public string language; }
    [Serializable] private sealed class GuideResponse { public string summary; public string recommendedDifficulty; public string difficultyRationale; public string recommendedLayout; public string layoutRationale; public string source; }
    [Serializable]
    private sealed class DraftRequest
    {
        public string q1Answer;
        public string q2Answer;
        public string q3Answer;
        public string q4Answer;
        public string aiReflection;
        public string aiDifficultyRationale;
        public string aiLayoutRationale;
        public string aiRecommendedDifficulty;
        public string aiRecommendedLayout;
        public string aiRecommendationSource;
        public string finalDifficulty;
        public string finalLayout;
    }
    [Serializable] private sealed class ErrorEnvelope { public string detail; }
}
