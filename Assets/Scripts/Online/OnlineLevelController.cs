using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

[DefaultExecutionOrder(-200)]
public class OnlineLevelController : MonoBehaviour
{
    private const string LobbySceneName = "Online_Lobby";
    private const string ResultSceneName = "Match_Result";

    [Header("Level")]
    [SerializeField] private LevelData levelData;
    [SerializeField] private LevelLoader levelLoader;
    [SerializeField] private LevelManager levelManager;
    [SerializeField] private LevelSolver levelSolver;

    [Header("UI")]
    [SerializeField] private Text roomCodeText;
    [SerializeField] private Text statusText;
    [SerializeField] private GameObject statusPanel;
    [SerializeField] private GameObject completePanel;
    [SerializeField] private Text completeHintText;
    [SerializeField] private Button leaveButton;
    [SerializeField] private Button completeLeaveButton;

    private OnlineMatchClient client;
    private bool leaving;
    private bool transitioning;
    private bool runStarted;
    private bool runCompleted;
    private bool resultSubmissionStarted;
    private bool resultSubmitted;
    private float runStartedAt;
    private float runDurationSeconds;
    private int runMoveCount;
    private int minimumMoves = -1;

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
            levelManager.CompletionTransitionRequested += HandleLevelCompleted;
        }

        LevelStudyRecorder.PlayerMoveRecorded += HandlePlayerMove;
    }

    private void Start()
    {
        OnlineSceneUi.EnsureEventSystem();
        ResolveReferences();
        WireUi();
        OnlineSceneUi.ConfigureRaycastTargets();

        if (completePanel != null)
        {
            completePanel.SetActive(false);
        }

        if (completeLeaveButton != null)
        {
            completeLeaveButton.interactable = false;
        }

        SetText(roomCodeText, "ROOM " + OnlineMatchContext.RoomCode);

        if (!OnlineMatchContext.HasMatch
            || !OnlineMatchContext.HasOpponentChallenge)
        {
            ShowFailure("NO OPPONENT CHALLENGE WAS FOUND.");
            return;
        }

        client = gameObject.GetComponent<OnlineMatchClient>();

        if (client == null)
        {
            client = gameObject.AddComponent<OnlineMatchClient>();
        }

        StartCoroutine(PrepareLevel());
    }

    private void OnDestroy()
    {
        if (levelManager != null)
        {
            levelManager.LevelSolved -= HandleLevelSolved;
            levelManager.CompletionTransitionRequested -= HandleLevelCompleted;
        }

        LevelStudyRecorder.PlayerMoveRecorded -= HandlePlayerMove;
    }

    private IEnumerator PrepareLevel()
    {
        string validationError;
        string[] rows = CloneRows(OnlineMatchContext.OpponentChallengeRows);

        if (!TryValidateRows(rows, out validationError))
        {
            ShowFailure(validationError);
            yield break;
        }

        levelData.rows = rows;
        levelSolver.levelData = levelData;
        levelSolver.maxSearchStates = 300000;

        if (!levelSolver.ParseLevel()
            || !levelSolver.CanSolve(
                out int searchedStates,
                out int solutionSteps,
                out int pushCount))
        {
            ShowFailure("THE OPPONENT CHALLENGE COULD NOT BE VERIFIED.");
            yield break;
        }

        minimumMoves = solutionSteps;
        SetPanelVisible(statusPanel, false);

        if (levelManager != null)
        {
            yield return levelManager.FadeToBlackForExternalInitialLoad();
        }

        levelLoader.levelData = levelData;
        levelLoader.LoadLevel();

        if (levelManager != null)
        {
            yield return levelManager.FadeFromBlackAfterExternalInitialLoad(
                false
            );
        }

        runMoveCount = 0;
        runStartedAt = Time.realtimeSinceStartup;
        runStarted = true;

        if (levelManager != null)
        {
            levelManager.SetExternalPlayerInputEnabled(true);
        }
    }

    private void HandleLevelSolved(LevelManager manager)
    {
        if (!runStarted || runCompleted)
        {
            return;
        }

        runCompleted = true;
        runDurationSeconds = Mathf.Round(
            Mathf.Max(0f, Time.realtimeSinceStartup - runStartedAt) * 100f
        ) / 100f;
    }

    private void HandlePlayerMove(bool pushedBox)
    {
        if (runStarted && !runCompleted)
        {
            runMoveCount++;
        }
    }

    private void HandleLevelCompleted(LevelManager manager)
    {
        manager.MarkCompletionTransitionHandled();
        SetPanelVisible(completePanel, true);
        SetPanelVisible(statusPanel, false);

        if (!runCompleted)
        {
            HandleLevelSolved(manager);
        }

        if (resultSubmissionStarted)
        {
            return;
        }

        resultSubmissionStarted = true;
        StartCoroutine(SubmitResultUntilSuccessful());
    }

    private IEnumerator SubmitResultUntilSuccessful()
    {
        while (!leaving && !transitioning)
        {
            SetText(completeHintText, "SUBMITTING RESULT...");
            bool succeeded = false;

            yield return client.SubmitResult(
                runDurationSeconds,
                runMoveCount,
                minimumMoves,
                state =>
                {
                    succeeded = true;
                    OnlineMatchContext.ApplyState(state);
                },
                error => SetText(
                    completeHintText,
                    "RESULT SUBMISSION FAILED. RETRYING...\n"
                    + error.ToUpperInvariant()
                )
            );

            if (succeeded)
            {
                if (!Application.CanStreamedLevelBeLoaded(ResultSceneName))
                {
                    SetText(
                        completeHintText,
                        "MATCH RESULT SCENE IS NOT AVAILABLE."
                    );
                    yield break;
                }

                resultSubmitted = true;
                SetText(
                    completeHintText,
                    "RESULT SUBMITTED.\nSELECT LEAVE TO VIEW MATCH RESULTS."
                );

                if (completeLeaveButton != null)
                {
                    completeLeaveButton.interactable = true;
                }

                yield break;
            }

            yield return new WaitForSecondsRealtime(1f);
        }
    }

    private void OpenMatchResult()
    {
        if (leaving || transitioning || !resultSubmitted)
        {
            return;
        }

        if (!Application.CanStreamedLevelBeLoaded(ResultSceneName))
        {
            SetText(completeHintText, "MATCH RESULT SCENE IS NOT AVAILABLE.");
            return;
        }

        transitioning = true;

        if (leaveButton != null)
        {
            leaveButton.interactable = false;
        }

        if (completeLeaveButton != null)
        {
            completeLeaveButton.interactable = false;
        }

        SceneManager.LoadScene(ResultSceneName);
    }

    private void LeaveMatch()
    {
        if (leaving || transitioning)
        {
            return;
        }

        leaving = true;

        if (leaveButton != null)
        {
            leaveButton.interactable = false;
        }

        if (completeLeaveButton != null)
        {
            completeLeaveButton.interactable = false;
        }

        if (client == null || !OnlineMatchContext.HasMatch)
        {
            FinishLeaving();
            return;
        }

        StartCoroutine(
            client.LeaveRoom(
                state => FinishLeaving(),
                error => FinishLeaving()
            )
        );
    }

    private void FinishLeaving()
    {
        OnlineMatchContext.Clear();
        SceneManager.LoadScene(LobbySceneName);
    }

    private void ShowFailure(string message)
    {
        SetPanelVisible(statusPanel, true);
        SetPanelVisible(completePanel, false);
        SetText(statusText, message);
    }

    private void ResolveReferences()
    {
        levelData = levelData != null ? levelData : FindObjectOfType<LevelData>();
        levelLoader = levelLoader != null
            ? levelLoader
            : FindObjectOfType<LevelLoader>();
        levelManager = levelManager != null
            ? levelManager
            : FindObjectOfType<LevelManager>();
        levelSolver = levelSolver != null
            ? levelSolver
            : FindObjectOfType<LevelSolver>();
        roomCodeText = roomCodeText != null
            ? roomCodeText
            : OnlineSceneUi.FindText("RoomCodeText");
        statusText = statusText != null
            ? statusText
            : OnlineSceneUi.FindText("OnlineLevelStatusText");
        statusPanel = statusPanel != null
            ? statusPanel
            : GameObject.Find("OnlineLevelStatusPanel");
        completePanel = completePanel != null
            ? completePanel
            : GameObject.Find("ChallengeCompletePanel");
        completeHintText = completeHintText != null
            ? completeHintText
            : OnlineSceneUi.FindText("CompleteHint");
        leaveButton = leaveButton != null
            ? leaveButton
            : OnlineSceneUi.EnsureButton("LeaveMatchButton");
        completeLeaveButton = completeLeaveButton != null
            ? completeLeaveButton
            : OnlineSceneUi.EnsureButton("CompleteLeaveMatchButton");
    }

    private void WireUi()
    {
        if (leaveButton != null)
        {
            leaveButton.onClick.RemoveListener(LeaveMatch);
            leaveButton.onClick.AddListener(LeaveMatch);
        }

        if (completeLeaveButton != null)
        {
            completeLeaveButton.onClick.RemoveListener(LeaveMatch);
            completeLeaveButton.onClick.RemoveListener(OpenMatchResult);
            completeLeaveButton.onClick.AddListener(OpenMatchResult);
        }
    }

    private static bool TryValidateRows(string[] rows, out string error)
    {
        error = "";

        if (rows == null || rows.Length != 10)
        {
            error = "THE OPPONENT CHALLENGE MUST CONTAIN 10 ROWS.";
            return false;
        }

        int playerCount = 0;
        int boxCount = 0;
        int targetCount = 0;
        const string allowedTiles = " #.@pst";

        for (int row = 0; row < rows.Length; row++)
        {
            if (rows[row] == null || rows[row].Length != 12)
            {
                error = "EVERY OPPONENT CHALLENGE ROW MUST CONTAIN 12 TILES.";
                return false;
            }

            for (int column = 0; column < rows[row].Length; column++)
            {
                char tile = rows[row][column];

                if (allowedTiles.IndexOf(tile) < 0)
                {
                    error = "THE OPPONENT CHALLENGE CONTAINS AN UNKNOWN TILE.";
                    return false;
                }

                playerCount += tile == LevelData.Player ? 1 : 0;
                boxCount += tile == LevelData.Box ? 1 : 0;
                targetCount += tile == LevelData.Target ? 1 : 0;
            }
        }

        if (playerCount != 1
            || boxCount < 1
            || boxCount > 2
            || targetCount != boxCount)
        {
            error = "THE OPPONENT CHALLENGE HAS INVALID PLAYER OR BOX DATA.";
            return false;
        }

        return true;
    }

    private static string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return null;
        }

        string[] clone = new string[rows.Length];

        for (int row = 0; row < rows.Length; row++)
        {
            clone[row] = rows[row] ?? "";
        }

        return clone;
    }

    private static void SetPanelVisible(GameObject panel, bool visible)
    {
        if (panel != null)
        {
            panel.SetActive(visible);
        }
    }

    private static void SetText(Text text, string value)
    {
        if (text != null)
        {
            text.text = value;
        }
    }
}
