using System;

public static class CoCreationDraftContext
{
    public static string[] Rows { get; private set; }
    public static string InitialDraftMethod { get; private set; } = "";
    public static string CreationKey { get; private set; } = "";
    public static string SessionId { get; private set; } = "";
    public static string IntegrationToken { get; private set; } = "";

    public static bool HasDraft =>
        Rows != null
        && Rows.Length == 10
        && !string.IsNullOrWhiteSpace(InitialDraftMethod);

    public static void Stage(string[] rows, string initialDraftMethod)
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
    }

    public static void RecordSession(string sessionId, string integrationToken)
    {
        SessionId = sessionId ?? "";
        IntegrationToken = integrationToken ?? "";
    }

    public static void Clear()
    {
        Rows = null;
        InitialDraftMethod = "";
        CreationKey = "";
        SessionId = "";
        IntegrationToken = "";
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
