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
    private string errorMessage = "";
    private CoCreationStagePlayView playView;

#if UNITY_WEBGL && !UNITY_EDITOR
    [DllImport("__Internal")]
    private static extern void SokobanNavigateCurrentPage(string url);
#endif

    private void Awake()
    {
        ResolveReferences();
        playView = FindObjectOfType<CoCreationStagePlayView>(true);

        if (playView != null)
        {
            playView.Bind(IsChinese(), ReturnToLab);
        }
        else
        {
            Debug.LogError(
                "CoCreationStagePlayController: The static Stage Play UI is missing."
            );
        }

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
            ShowError("No valid co-creation Play context was found.");
            yield break;
        }

        ResolveReferences();

        if (levelData == null || levelLoader == null || levelManager == null || levelSolver == null)
        {
            ShowError("The selected Stage cannot be loaded because scene references are missing.");
            yield break;
        }

        if (!TryValidateRows(CoCreationPlayContext.Rows, out string validationError))
        {
            ShowError(validationError);
            yield break;
        }

        levelData.rows = CloneRows(CoCreationPlayContext.Rows);
        levelSolver.levelData = levelData;
        levelSolver.maxSearchStates = 300000;

        int searchedStates = 0;
        if (!levelSolver.ParseLevel()
            || !levelSolver.CanSolve(out searchedStates, out minimumMoves, out minimumPushes))
        {
            ShowError(
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

        if (playView != null)
        {
            playView.Unbind();
        }
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
        playView?.SetStatus(
            IsChinese()
                ? "PLAY COMPLETED / 试玩完成，结果已记录"
                : "PLAY COMPLETED — RESULT RECORDED."
        );
        StartCoroutine(SendMetrics("complete"));
    }

    private void HandleCompletionTransition(LevelManager manager)
    {
        manager.MarkCompletionTransitionHandled();
        StartCoroutine(manager.FadeFromBlackAfterExternalInitialLoad(false));
    }

    private void ReturnToLab()
    {
        if (returning)
        {
            return;
        }

        returning = true;
        playView?.SetReturnInteractable(false);
        if (levelManager != null)
        {
            levelManager.SetExternalPlayerInputEnabled(false);
        }
        StartCoroutine(ReturnRoutine());
    }

    private IEnumerator ReturnRoutine()
    {
        if (completed)
        {
            yield return SendMetrics("complete");
        }
        else
        {
            yield return SendMetrics("abandon");
        }

        string returnUrl = CoCreationPlayContext.ReturnUrl;
        CoCreationPlayContext.Clear();

        if (string.IsNullOrWhiteSpace(returnUrl))
        {
            returning = false;
            playView?.SetReturnInteractable(true);
            ShowError("The co-creation return URL is missing.");
            yield break;
        }

#if UNITY_WEBGL && !UNITY_EDITOR
        SokobanNavigateCurrentPage(returnUrl);
#else
        Application.OpenURL(returnUrl);
#endif
    }

    private IEnumerator SendMetrics(string action)
    {
        if (string.IsNullOrWhiteSpace(CoCreationPlayContext.AttemptId)
            || string.IsNullOrWhiteSpace(CoCreationPlayContext.AttemptToken))
        {
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
            request.timeout = Mathf.Max(1, requestTimeoutSeconds);
            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning(
                    "CoCreationStagePlayController: Could not submit "
                    + action + " metrics: " + request.error
                );
            }
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

    private void ShowError(string message)
    {
        errorMessage = IsChinese() ? "无法加载试玩：" + message : "PLAY UNAVAILABLE: " + message;
        playView?.SetStatus(errorMessage);
        Debug.LogError("CoCreationStagePlayController: " + message);
        if (levelManager != null)
        {
            levelManager.SetExternalPlayerInputEnabled(false);
        }
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

    private static bool IsChinese()
    {
        return string.Equals(CoCreationPlayContext.Language, "zh-CN", StringComparison.Ordinal);
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
