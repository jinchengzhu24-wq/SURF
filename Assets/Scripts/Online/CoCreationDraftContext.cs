using System;

public static class CoCreationDraftContext
{
    public static string[] Rows { get; private set; }
    public static string InitialDraftMethod { get; private set; } = "";
    public static string CreationKey { get; private set; } = "";
    public static string SessionId { get; private set; } = "";
    public static string IntegrationToken { get; private set; } = "";
    public static string RegenerationRequestId { get; private set; } = "";
    public static string RegenerationFailureCode { get; private set; } = "";
    public static bool RegenerationResultReady { get; private set; }
    private static LevelDesignPlan savedLevelDesignPlan;
    private static string[] regeneratedRows;

    public static bool HasSavedLevelDesignPlan => savedLevelDesignPlan != null;
    public static string[] RegeneratedRows => regeneratedRows == null
        ? null
        : CloneRows(regeneratedRows);

    public static bool HasDraft =>
        Rows != null
        && Rows.Length == 10
        && !string.IsNullOrWhiteSpace(InitialDraftMethod);

    public static bool HasSession =>
        HasDraft
        && !string.IsNullOrWhiteSpace(SessionId)
        && !string.IsNullOrWhiteSpace(IntegrationToken);

    public static bool IsRegenerating =>
        HasSession
        && !string.IsNullOrWhiteSpace(RegenerationRequestId)
        && !RegenerationResultReady;

    public static bool HasRegenerationResult =>
        HasSession
        && !string.IsNullOrWhiteSpace(RegenerationRequestId)
        && RegenerationResultReady;

    public static void Stage(
        string[] rows,
        string initialDraftMethod,
        LevelDesignPlan levelDesignPlan = null)
    {
        if (rows == null || rows.Length != 10)
        {
            throw new ArgumentException("A complete 10-row draft is required.");
        }

        Rows = CloneRows(rows);
        InitialDraftMethod = initialDraftMethod ?? "";
        CreationKey = "unity_" + Guid.NewGuid().ToString("N");
        SessionId = "";
        IntegrationToken = "";
        savedLevelDesignPlan = levelDesignPlan == null ? null : levelDesignPlan.Copy();
        ClearRegeneration();
    }

    public static LevelDesignPlan GetSavedLevelDesignPlan()
    {
        return savedLevelDesignPlan == null ? null : savedLevelDesignPlan.Copy();
    }

    public static void RecordSession(string sessionId, string integrationToken)
    {
        SessionId = sessionId ?? "";
        IntegrationToken = integrationToken ?? "";
    }

    public static void BeginRegeneration(string requestId)
    {
        if (!HasSession || string.IsNullOrWhiteSpace(requestId))
        {
            throw new InvalidOperationException("A session and regeneration request are required.");
        }

        RegenerationRequestId = requestId.Trim();
        RegenerationFailureCode = "";
        RegenerationResultReady = false;
    }

    public static void CompleteRegeneration(string[] rows)
    {
        if (!IsRegenerating || rows == null || rows.Length != 10)
        {
            throw new InvalidOperationException("A complete regenerated draft is required.");
        }

        regeneratedRows = CloneRows(rows);
        RegenerationFailureCode = "";
        RegenerationResultReady = true;
    }

    public static void AcceptRegenerationResult()
    {
        if (!HasRegenerationResult
            || !string.IsNullOrWhiteSpace(RegenerationFailureCode)
            || regeneratedRows == null)
        {
            return;
        }

        Rows = CloneRows(regeneratedRows);
    }

    public static void FailRegeneration(string failureCode)
    {
        if (!IsRegenerating)
        {
            return;
        }

        RegenerationFailureCode = string.IsNullOrWhiteSpace(failureCode)
            ? "generation_failed"
            : failureCode.Trim();
        RegenerationResultReady = true;
    }

    public static void ClearRegeneration()
    {
        RegenerationRequestId = "";
        RegenerationFailureCode = "";
        RegenerationResultReady = false;
        regeneratedRows = null;
    }

    public static void Clear()
    {
        Rows = null;
        InitialDraftMethod = "";
        CreationKey = "";
        SessionId = "";
        IntegrationToken = "";
        savedLevelDesignPlan = null;
        ClearRegeneration();
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
