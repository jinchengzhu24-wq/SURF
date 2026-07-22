using UnityEngine;

public static class CreativeWorkshopContext
{
    public const string IdeaIdPrefsKey = "SokobanCreativeWorkshopIdeaId";
    public const string SessionIdPrefsKey = "SokobanCreativeWorkshopIdeaSessionId";
    public const string IdeaTextPrefsKey = "SokobanCreativeWorkshopIdeaText";
    public const string OriginalIdeaTextPrefsKey = "SokobanCreativeWorkshopOriginalIdeaText";
    public const string SelectedDirectionTextPrefsKey = "SokobanCreativeWorkshopSelectedDirectionText";
    public const string RefinementFeedbackTextPrefsKey = "SokobanCreativeWorkshopRefinementFeedbackText";
    public const string AdjustmentHistoryTextPrefsKey = "SokobanCreativeWorkshopAdjustmentHistoryText";
    public const string LatestAdjustmentTextPrefsKey = "SokobanCreativeWorkshopLatestAdjustmentText";
    public const string RevisionModePrefsKey = "SokobanCreativeWorkshopRevisionMode";
    public const string PreviousLevelPlanPrefsKey = "SokobanCreativeWorkshopPreviousLevelPlan";
    public const string PreviousLevelMetricsPrefsKey = "SokobanCreativeWorkshopPreviousLevelMetrics";
    public const string PendingHumanAdjustmentPrefsKey = "SokobanPendingHumanAdjustment";
    public const string HumanClarityReasonPrefsKey = "SokobanHumanClarityReason";

    public static string IdeaId { get; private set; }
    public static string SessionId { get; private set; }
    public static string IdeaText { get; private set; }
    public static string OriginalIdeaText { get; private set; }
    public static string SelectedDirectionText { get; private set; }
    public static string RefinementFeedbackText { get; private set; }
    public static string AdjustmentHistoryText { get; private set; }
    public static string LatestAdjustmentText { get; private set; }
    public static string RevisionMode { get; private set; }

    public static bool HasIdea
    {
        get { return !string.IsNullOrEmpty(IdeaText); }
    }

    public static void SetIdea(string ideaId, string sessionId, string ideaText)
    {
        IdeaId = ideaId ?? "";
        SessionId = sessionId ?? "";
        IdeaText = ideaText ?? "";

        PlayerPrefs.SetString(IdeaIdPrefsKey, IdeaId);
        PlayerPrefs.SetString(SessionIdPrefsKey, SessionId);
        PlayerPrefs.SetString(IdeaTextPrefsKey, IdeaText);
        PlayerPrefs.Save();
    }

    public static void BeginIdea(string ideaId, string sessionId, string ideaText)
    {
        OriginalIdeaText = ideaText ?? "";
        SelectedDirectionText = "";
        RefinementFeedbackText = "";
        AdjustmentHistoryText = "";
        LatestAdjustmentText = "";
        RevisionMode = "";
        PlayerPrefs.DeleteKey(RevisionModePrefsKey);
        PlayerPrefs.DeleteKey(PreviousLevelPlanPrefsKey);
        PlayerPrefs.DeleteKey(PreviousLevelMetricsPrefsKey);
        PlayerPrefs.DeleteKey(PendingHumanAdjustmentPrefsKey);
        PlayerPrefs.DeleteKey(HumanClarityReasonPrefsKey);
        SaveStructuredContext();
        SetIdea(ideaId, sessionId, ideaText);
    }

    public static void SetSelectedDirection(
        string ideaId,
        string sessionId,
        string selectedDirectionText,
        string combinedIdeaText)
    {
        SelectedDirectionText = selectedDirectionText ?? "";
        PlayerPrefs.SetString(SelectedDirectionTextPrefsKey, SelectedDirectionText);
        SetIdea(ideaId, sessionId, combinedIdeaText);
    }

    public static void SetRefinementFeedback(
        string ideaId,
        string sessionId,
        string feedbackText,
        string combinedIdeaText)
    {
        RefinementFeedbackText = AppendLine(
            GetValue(RefinementFeedbackText, RefinementFeedbackTextPrefsKey),
            feedbackText
        );
        PlayerPrefs.SetString(RefinementFeedbackTextPrefsKey, RefinementFeedbackText);
        SetIdea(ideaId, sessionId, combinedIdeaText);
    }

    public static void AppendAdjustment(
        string ideaId,
        string sessionId,
        string adjustmentText,
        string combinedIdeaText)
    {
        string previousLatest = GetValue(LatestAdjustmentText, LatestAdjustmentTextPrefsKey);
        AdjustmentHistoryText = GetValue(AdjustmentHistoryText, AdjustmentHistoryTextPrefsKey);

        if (!string.IsNullOrWhiteSpace(previousLatest))
        {
            AdjustmentHistoryText = AppendLine(AdjustmentHistoryText, previousLatest);
        }

        LatestAdjustmentText = adjustmentText == null ? "" : adjustmentText.Trim();
        PlayerPrefs.SetString(AdjustmentHistoryTextPrefsKey, AdjustmentHistoryText);
        PlayerPrefs.SetString(LatestAdjustmentTextPrefsKey, LatestAdjustmentText);
        SetIdea(ideaId, sessionId, combinedIdeaText);
    }

    public static void SetRevisionMode(string revisionMode)
    {
        RevisionMode = string.IsNullOrWhiteSpace(revisionMode)
            ? ""
            : revisionMode.Trim().ToLowerInvariant();
        PlayerPrefs.SetString(RevisionModePrefsKey, RevisionMode);
        PlayerPrefs.Save();
    }

    public static void SetPreviousLevelPlan(string planJson)
    {
        PlayerPrefs.SetString(PreviousLevelPlanPrefsKey, planJson ?? "");
        PlayerPrefs.Save();
    }

    public static void SetPreviousLevelMetrics(string metricsJson)
    {
        PlayerPrefs.SetString(PreviousLevelMetricsPrefsKey, metricsJson ?? "");
        PlayerPrefs.Save();
    }

    public static void SetPendingHumanAdjustment(string adjustmentText, string clarityReason)
    {
        PlayerPrefs.SetString(PendingHumanAdjustmentPrefsKey, adjustmentText ?? "");
        PlayerPrefs.SetString(HumanClarityReasonPrefsKey, clarityReason ?? "");
        PlayerPrefs.Save();
    }

    public static void ClearPendingHumanAdjustment()
    {
        PlayerPrefs.DeleteKey(PendingHumanAdjustmentPrefsKey);
        PlayerPrefs.DeleteKey(HumanClarityReasonPrefsKey);
        PlayerPrefs.Save();
    }

    public static void Clear()
    {
        IdeaId = "";
        SessionId = "";
        IdeaText = "";
        OriginalIdeaText = "";
        SelectedDirectionText = "";
        RefinementFeedbackText = "";
        AdjustmentHistoryText = "";
        LatestAdjustmentText = "";
        RevisionMode = "";

        PlayerPrefs.DeleteKey(IdeaIdPrefsKey);
        PlayerPrefs.DeleteKey(SessionIdPrefsKey);
        PlayerPrefs.DeleteKey(IdeaTextPrefsKey);
        PlayerPrefs.DeleteKey(OriginalIdeaTextPrefsKey);
        PlayerPrefs.DeleteKey(SelectedDirectionTextPrefsKey);
        PlayerPrefs.DeleteKey(RefinementFeedbackTextPrefsKey);
        PlayerPrefs.DeleteKey(AdjustmentHistoryTextPrefsKey);
        PlayerPrefs.DeleteKey(LatestAdjustmentTextPrefsKey);
        PlayerPrefs.DeleteKey(RevisionModePrefsKey);
        PlayerPrefs.DeleteKey(PreviousLevelPlanPrefsKey);
        PlayerPrefs.DeleteKey(PreviousLevelMetricsPrefsKey);
        PlayerPrefs.DeleteKey(PendingHumanAdjustmentPrefsKey);
        PlayerPrefs.DeleteKey(HumanClarityReasonPrefsKey);
        PlayerPrefs.Save();
    }

    private static void SaveStructuredContext()
    {
        PlayerPrefs.SetString(OriginalIdeaTextPrefsKey, OriginalIdeaText);
        PlayerPrefs.SetString(SelectedDirectionTextPrefsKey, SelectedDirectionText);
        PlayerPrefs.SetString(RefinementFeedbackTextPrefsKey, RefinementFeedbackText);
        PlayerPrefs.SetString(AdjustmentHistoryTextPrefsKey, AdjustmentHistoryText);
        PlayerPrefs.SetString(LatestAdjustmentTextPrefsKey, LatestAdjustmentText);
        PlayerPrefs.Save();
    }

    private static string GetValue(string runtimeValue, string prefsKey)
    {
        return !string.IsNullOrEmpty(runtimeValue)
            ? runtimeValue
            : PlayerPrefs.GetString(prefsKey, "");
    }

    private static string AppendLine(string existing, string value)
    {
        string cleanValue = value == null ? "" : value.Trim();

        if (string.IsNullOrEmpty(cleanValue))
        {
            return existing ?? "";
        }

        return string.IsNullOrWhiteSpace(existing)
            ? cleanValue
            : existing.Trim() + "\n" + cleanValue;
    }
}
