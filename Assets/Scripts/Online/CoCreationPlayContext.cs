using System;

public static class CoCreationPlayContext
{
    public static string AttemptId { get; private set; } = "";
    public static string SessionId { get; private set; } = "";
    public static string VersionId { get; private set; } = "";
    public static string[] Rows { get; private set; }
    public static string InitialDraftMethod { get; private set; } = "";
    public static string Language { get; private set; } = "en";
    public static string AttemptToken { get; private set; } = "";
    public static string ReturnUrl { get; private set; } = "";

    public static bool IsActive =>
        !string.IsNullOrWhiteSpace(AttemptId)
        && !string.IsNullOrWhiteSpace(AttemptToken)
        && Rows != null
        && Rows.Length == 10;

    public static void Initialize(CoCreationPlayBootstrapResponse response)
    {
        if (response == null || response.rows == null || response.rows.Length != 10)
        {
            throw new ArgumentException("The Play bootstrap response is incomplete.");
        }

        AttemptId = response.attemptId ?? "";
        SessionId = response.sessionId ?? "";
        VersionId = response.versionId ?? "";
        Rows = CloneRows(response.rows);
        InitialDraftMethod = response.initialDraftMethod ?? "";
        Language = response.language == "zh-CN" ? "zh-CN" : "en";
        AttemptToken = response.attemptToken ?? "";
        ReturnUrl = response.returnUrl ?? "";
    }

    public static string ResolveSceneName()
    {
        return string.Equals(
            InitialDraftMethod,
            AIAssistantModeController.PartialCompletionApiMode,
            StringComparison.OrdinalIgnoreCase)
            ? "PC_Level"
            : "DG_Level";
    }

    public static void Clear()
    {
        AttemptId = "";
        SessionId = "";
        VersionId = "";
        Rows = null;
        InitialDraftMethod = "";
        Language = "en";
        AttemptToken = "";
        ReturnUrl = "";
    }

    private static string[] CloneRows(string[] rows)
    {
        string[] clone = new string[rows.Length];

        for (int index = 0; index < rows.Length; index++)
        {
            clone[index] = rows[index] ?? "";
        }

        return clone;
    }
}

[Serializable]
public sealed class CoCreationPlayBootstrapResponse
{
    public string attemptId;
    public string sessionId;
    public string versionId;
    public string[] rows;
    public string initialDraftMethod;
    public string language;
    public string attemptToken;
    public string returnUrl;
}
