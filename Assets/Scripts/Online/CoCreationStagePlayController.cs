using System;
using System.Collections;
using System.Runtime.InteropServices;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

[DefaultExecutionOrder(-400)]
public sealed class CoCreationStagePlayController : MonoBehaviour
{
    private const string DefaultBackendBaseUrl = "http://111.231.136.4:8010";
    private const int CompletionSubmitMaxAttempts = 2;
    private const int CompletionRequestTimeoutSeconds = 5;
    private const float CompletionRetryDelaySeconds = 0.5f;

    [SerializeField] private string backendBaseUrl = DefaultBackendBaseUrl;
    [SerializeField] private int requestTimeoutSeconds = 10;

    private LevelData levelData;
    private LevelLoader levelLoader;
    private LevelManager levelManager;
    private LevelSolver levelSolver;
    private bool levelReady;
    private bool completed;
    private bool returning;
    private bool firstMoveRecorded;
    private float firstMoveAt;
    private float completedDuration;
    private int moveCount;
    private int pushCount;
    private int restartCount;
    private int minimumMoves = -1;
    private int minimumPushes = -1;

#if UNITY_WEBGL && !UNITY_EDITOR
    [DllImport("__Internal")]
    private static extern void SokobanNavigateCurrentPage(string url);
#endif

    private void Awake()
    {
        ResolveReferences();

        if (levelLoader != null)
        {
            levelLoader.deferLoadToExternalController = true;
        }

        if (levelManager != null)
        {
            levelManager.BeginExternalInitialLoadingTransition();
            levelManager.LevelSolved += HandleLevelSolved;
            levelManager.CompletionTransitionRequested += HandleCompletionTransition;
        }

        LevelStudyRecorder.PlayerMoveRecorded += HandlePlayerMove;
        LevelStudyRecorder.LevelRestarted += HandleRestart;
    }

    private IEnumerator Start()
    {
        if (!CoCreationPlayContext.IsActive)
        {
            HandleLoadFailure("No valid co-creation Play context was found.");
            yield break;
        }

        ResolveReferences();

        if (levelData == null || levelLoader == null || levelManager == null || levelSolver == null)
        {
            HandleLoadFailure(
                "The selected Stage cannot be loaded because scene references are missing."
            );
            yield break;
        }

        if (!TryValidateRows(CoCreationPlayContext.Rows, out string validationError))
        {
            HandleLoadFailure(validationError);
            yield break;
        }

        levelData.rows = CloneRows(CoCreationPlayContext.Rows);
        levelSolver.levelData = levelData;
        levelSolver.maxSearchStates = 300000;

        int searchedStates = 0;
        if (!levelSolver.ParseLevel()
            || !levelSolver.CanSolve(out searchedStates, out minimumMoves, out minimumPushes))
        {
            HandleLoadFailure(
                "Unity could not verify this Stage. searchedStates=" + searchedStates
            );
            yield break;
        }

        yield return levelManager.FadeToBlackForExternalInitialLoad();
        levelLoader.levelData = levelData;
        levelLoader.LoadLevel();
        yield return levelManager.FadeFromBlackAfterExternalInitialLoad(false);
        levelReady = true;
        levelManager.SetExternalPlayerInputEnabled(true);
        StartCoroutine(SendMetrics("start"));
    }

    private void OnDestroy()
    {
        if (levelManager != null)
        {
            levelManager.LevelSolved -= HandleLevelSolved;
            levelManager.CompletionTransitionRequested -= HandleCompletionTransition;
        }

        LevelStudyRecorder.PlayerMoveRecorded -= HandlePlayerMove;
        LevelStudyRecorder.LevelRestarted -= HandleRestart;
    }

    private void HandlePlayerMove(bool pushedBox)
    {
        if (!levelReady || completed || returning)
        {
            return;
        }

        if (!firstMoveRecorded)
        {
            firstMoveRecorded = true;
            firstMoveAt = Time.realtimeSinceStartup;
        }

        moveCount++;

        if (pushedBox)
        {
            pushCount++;
        }

        if (moveCount % 10 == 0)
        {
            StartCoroutine(SendMetrics("progress"));
        }
    }

    private void HandleRestart()
    {
        if (!levelReady || completed || returning)
        {
            return;
        }

        restartCount++;
        StartCoroutine(SendMetrics("progress"));
    }

    private void HandleLevelSolved(LevelManager manager)
    {
        if (!levelReady || completed)
        {
            return;
        }

        completed = true;
        completedDuration = GetActiveDuration();
    }

    private void HandleCompletionTransition(LevelManager manager)
    {
        manager.MarkCompletionTransitionHandled();

        if (returning)
        {
            return;
        }

        returning = true;
        manager.SetExternalPlayerInputEnabled(false);
        StartCoroutine(CompleteAndReturnRoutine());
    }

    private IEnumerator CompleteAndReturnRoutine()
    {
        bool submitted = false;

        for (int attempt = 1; attempt <= CompletionSubmitMaxAttempts; attempt++)
        {
            yield return SendMetrics(
                "complete",
                CompletionRequestTimeoutSeconds,
                "completed",
                success => submitted = success
            );

            if (submitted)
            {
                break;
            }

            if (attempt < CompletionSubmitMaxAttempts)
            {
                yield return new WaitForSecondsRealtime(
                    CompletionRetryDelaySeconds
                );
            }
        }

        NavigateToLab(submitted ? "" : "sync_failed");
    }

    private void HandleLoadFailure(string message)
    {
        Debug.LogError("CoCreationStagePlayController: " + message);

        if (levelManager != null)
        {
            levelManager.SetExternalPlayerInputEnabled(false);
        }

        if (returning)
        {
            return;
        }

        returning = true;
        StartCoroutine(ReturnAfterLoadFailureRoutine());
    }

    private IEnumerator ReturnAfterLoadFailureRoutine()
    {
        yield return null;
        NavigateToLab("load_failed");
    }

    private void NavigateToLab(string playReturnStatus)
    {
        string returnUrl = ResolveReturnUrl(playReturnStatus);
        CoCreationPlayContext.Clear();

        if (string.IsNullOrWhiteSpace(returnUrl))
        {
            Debug.LogError(
                "CoCreationStagePlayController: No safe co-creation return URL is available."
            );
            return;
        }

#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanNavigateCurrentPage(returnUrl);
#else
        Application.OpenURL(returnUrl);
#endif
    }

    private IEnumerator SendMetrics(string action)
    {
        yield return SendMetrics(
            action,
            requestTimeoutSeconds,
            "",
            null
        );
    }

    private IEnumerator SendMetrics(
        string action,
        int timeoutSeconds,
        string expectedStatus,
        Action<bool> onComplete)
    {
        if (string.IsNullOrWhiteSpace(CoCreationPlayContext.AttemptId)
            || string.IsNullOrWhiteSpace(CoCreationPlayContext.AttemptToken))
        {
            onComplete?.Invoke(false);
            yield break;
        }

        PlayMetricsRequest payload = new PlayMetricsRequest
        {
            attemptToken = CoCreationPlayContext.AttemptToken,
            durationSeconds = completed ? completedDuration : GetActiveDuration(),
            moveCount = moveCount,
            pushCount = pushCount,
            restartCount = restartCount,
            minimumMoves = minimumMoves,
            minimumPushes = minimumPushes
        };
        string endpoint = backendBaseUrl.TrimEnd('/')
            + "/api/play-attempts/"
            + UnityWebRequest.EscapeURL(CoCreationPlayContext.AttemptId)
            + "/"
            + action;

        using (UnityWebRequest request = new UnityWebRequest(endpoint, "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(
                Encoding.UTF8.GetBytes(JsonUtility.ToJson(payload))
            );
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.timeout = Mathf.Max(1, timeoutSeconds);
            yield return request.SendWebRequest();

            bool succeeded = request.result == UnityWebRequest.Result.Success;

            if (succeeded && !string.IsNullOrWhiteSpace(expectedStatus))
            {
                try
                {
                    PlayMetricsResponse response =
                        JsonUtility.FromJson<PlayMetricsResponse>(
                            request.downloadHandler.text
                        );
                    succeeded = response != null
                        && string.Equals(
                            response.status,
                            expectedStatus,
                            StringComparison.Ordinal
                        );
                }
                catch (Exception exception)
                {
                    succeeded = false;
                    Debug.LogWarning(
                        "CoCreationStagePlayController: Invalid "
                        + action
                        + " response: "
                        + exception.Message
                    );
                }
            }

            if (!succeeded)
            {
                Debug.LogWarning(
                    "CoCreationStagePlayController: Could not submit "
                    + action
                    + " metrics: "
                    + request.error
                    + " response="
                    + request.downloadHandler.text
                );
            }

            onComplete?.Invoke(succeeded);
        }
    }

    private void ResolveReferences()
    {
        levelData = levelData != null ? levelData : FindObjectOfType<LevelData>();
        levelLoader = levelLoader != null ? levelLoader : FindObjectOfType<LevelLoader>();
        levelManager = levelManager != null ? levelManager : FindObjectOfType<LevelManager>();
        levelSolver = levelSolver != null ? levelSolver : FindObjectOfType<LevelSolver>();
    }

    private float GetActiveDuration()
    {
        return firstMoveRecorded
            ? Mathf.Max(0f, Time.realtimeSinceStartup - firstMoveAt)
            : 0f;
    }

    private string ResolveReturnUrl(string playReturnStatus)
    {
        string candidate = CoCreationPlayContext.ReturnUrl;

        if (!TryGetSafeHttpUri(candidate, out Uri returnUri))
        {
            candidate = BuildFallbackReturnUrl();

            if (!TryGetSafeHttpUri(candidate, out returnUri))
            {
                return "";
            }
        }

        if (string.IsNullOrWhiteSpace(playReturnStatus))
        {
            return returnUri.AbsoluteUri;
        }

        UriBuilder builder = new UriBuilder(returnUri);
        string fragment = builder.Fragment.TrimStart('#');
        builder.Fragment = string.IsNullOrWhiteSpace(fragment)
            ? "playReturn=" + Uri.EscapeDataString(playReturnStatus)
            : fragment
                + "&playReturn="
                + Uri.EscapeDataString(playReturnStatus);
        return builder.Uri.AbsoluteUri;
    }

    private string BuildFallbackReturnUrl()
    {
        if (!TryGetSafeHttpUri(backendBaseUrl, out Uri baseUri))
        {
            return "";
        }

        UriBuilder builder = new UriBuilder(baseUri);
        builder.Path = builder.Path.TrimEnd('/') + "/";
        builder.Query = "";

        if (!string.IsNullOrWhiteSpace(CoCreationPlayContext.SessionId))
        {
            string fragment = "session="
                + Uri.EscapeDataString(CoCreationPlayContext.SessionId);

            if (!string.IsNullOrWhiteSpace(CoCreationPlayContext.VersionId))
            {
                fragment += "&stage="
                    + Uri.EscapeDataString(CoCreationPlayContext.VersionId);
            }

            builder.Fragment = fragment;
        }

        return builder.Uri.AbsoluteUri;
    }

    private static bool TryGetSafeHttpUri(string value, out Uri uri)
    {
        uri = null;

        if (!Uri.TryCreate(value, UriKind.Absolute, out Uri parsed)
            || (parsed.Scheme != Uri.UriSchemeHttp
                && parsed.Scheme != Uri.UriSchemeHttps))
        {
            return false;
        }

        uri = parsed;
        return true;
    }

    private static bool TryValidateRows(string[] rows, out string error)
    {
        if (rows == null || rows.Length != 10)
        {
            error = "The Stage must contain exactly 10 rows.";
            return false;
        }

        const string allowed = " #.@pst";
        for (int index = 0; index < rows.Length; index++)
        {
            if (rows[index] == null || rows[index].Length != 12)
            {
                error = "Every Stage row must contain exactly 12 cells.";
                return false;
            }

            foreach (char tile in rows[index])
            {
                if (allowed.IndexOf(tile) < 0)
                {
                    error = "The Stage contains an unsupported tile.";
                    return false;
                }
            }
        }

        error = "";
        return true;
    }

    private static string[] CloneRows(string[] rows)
    {
        string[] clone = new string[rows.Length];
        Array.Copy(rows, clone, rows.Length);
        return clone;
    }
}

[Serializable]
public sealed class PlayMetricsRequest
{
    public string attemptToken;
    public float durationSeconds;
    public int moveCount;
    public int pushCount;
    public int restartCount;
    public int minimumMoves;
    public int minimumPushes;
}

[Serializable]
public sealed class PlayMetricsResponse
{
    public string attemptId;
    public string status;
}
