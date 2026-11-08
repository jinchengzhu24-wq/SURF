using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class ChallengeWaitingController : MonoBehaviour
{
    private const string LobbySceneName = "Online_Lobby";
    private const string OnlineLevelSceneName = "Online_Level";

    [SerializeField] private Text statusText;
    [SerializeField] private Text roomCodeText;
    [SerializeField] private Button playButton;
    [SerializeField] private Text playButtonText;
    [SerializeField] private Button leaveButton;
    [SerializeField] private Text titleText;
    [SerializeField] private Text subtitleText;
    [SerializeField] private Text ownLevelStatusText;
    [SerializeField] private Text partnerLevelStatusText;

    private OnlineMatchClient client;
    private bool leaving;
    private bool transitioning;
    private bool submitInFlight;

    private void Start()
    {
        OnlineSceneUi.EnsureEventSystem();
        WireUi();
        OnlineSceneUi.ConfigureRaycastTargets();

        if (!OnlineMatchContext.HasMatch
            || !OnlineMatchContext.HasPendingChallenge)
        {
            SetStatus("NO ACTIVE CHALLENGE WAS FOUND.");
            SetText(ownLevelStatusText, "NO ACTIVE LEVEL");
            SetText(partnerLevelStatusText, "UNAVAILABLE");
            SetPlayEnabled(false);
            return;
        }

        client = gameObject.GetComponent<OnlineMatchClient>();

        if (client == null)
        {
            client = gameObject.AddComponent<OnlineMatchClient>();
        }

        SetText(roomCodeText, "ROOM " + OnlineMatchContext.RoomCode);
        SetText(playButtonText, "PLAY PARTNER'S LEVEL");
        SetPlayEnabled(false);
        StartCoroutine(SynchronizeChallenge());
    }

    private void WireUi()
    {
        if (playButton != null)
        {
            playButton.onClick.RemoveListener(PlayOpponentLevel);
            playButton.onClick.AddListener(PlayOpponentLevel);
        }

        if (leaveButton != null)
        {
            leaveButton.onClick.RemoveListener(LeaveMatch);
            leaveButton.onClick.AddListener(LeaveMatch);
        }
    }

    private IEnumerator SynchronizeChallenge()
    {
        while (!leaving && !transitioning && OnlineMatchContext.HasMatch)
        {
            OnlinePlayerState currentPlayer = OnlineMatchContext.RoomState
                ?.FindPlayer(OnlineMatchContext.PlayerNumber);

            if (currentPlayer == null || !currentPlayer.challengeSubmitted)
            {
                yield return SubmitChallenge();
            }
            else
            {
                SetStatus("WAITING FOR OPPONENT TO FINISH CREATING...");
                yield return RefreshRoom();
            }

            if (!leaving && !transitioning)
            {
                yield return new WaitForSecondsRealtime(1f);
            }
        }
    }

    private IEnumerator SubmitChallenge()
    {
        if (submitInFlight)
        {
            yield break;
        }

        submitInFlight = true;
        SetText(ownLevelStatusText, "SENDING...");
        SetStatus("SUBMITTING YOUR CHALLENGE...");
        SetPlayEnabled(false);
        bool succeeded = false;

        yield return client.SubmitChallenge(
            OnlineMatchContext.PendingChallengeRows,
            OnlineMatchContext.PendingAiAssistantMode,
            state =>
            {
                succeeded = true;
                OnlineMatchContext.ApplyState(state);
                UpdateFromState(state);
            },
            error =>
            {
                SetText(ownLevelStatusText, "RETRYING...");
                SetStatus("SUBMISSION FAILED. RETRYING...\n" + error.ToUpperInvariant());
            }
        );

        submitInFlight = false;

        if (succeeded)
        {
            OnlineMatchContext.ClearPendingChallenge();
        }
    }

    private IEnumerator RefreshRoom()
    {
        yield return client.GetRoom(
            state =>
            {
                OnlineMatchContext.ApplyState(state);
                UpdateFromState(state);
            },
            error => SetStatus(
                "CONNECTION LOST. RETRYING...\n" + error.ToUpperInvariant()
            )
        );
    }

    private void UpdateFromState(OnlineRoomState state)
    {
        if (state == null)
        {
            return;
        }

        bool ownSubmitted = state.FindPlayer(OnlineMatchContext.PlayerNumber)
            ?.challengeSubmitted == true;
        SetText(ownLevelStatusText, ownSubmitted ? "SENT!" : "SENDING...");
        SetText(titleText, ownSubmitted ? "LEVEL SENT!" : "LEVEL READY!");
        SetText(subtitleText, ownSubmitted
            ? "Nice! Your level is ready for your partner!"
            : "Your level is saved! Sending it to your partner...");

        if (state.status == "cancelled")
        {
            SetText(partnerLevelStatusText, "PARTNER LEFT");
            SetStatus("YOUR OPPONENT LEFT. THIS MATCH WAS CANCELLED.");
            SetPlayEnabled(false);
            return;
        }

        if (OnlineMatchContext.HasOpponentChallenge
            && (
                state.status == "challenges_ready"
                || state.status == "waiting_for_results"
                || state.status == "results_ready"
            ))
        {
            SetText(partnerLevelStatusText, "READY TO PLAY!");
            SetStatus("OPPONENT HAS FINISHED. READY TO PLAY.");
            SetPlayEnabled(true);
            return;
        }

        SetText(partnerLevelStatusText, "STILL BUILDING...");
        SetStatus("WAITING FOR YOUR PARTNER...");
        SetPlayEnabled(false);
    }

    private void PlayOpponentLevel()
    {
        if (leaving
            || transitioning
            || !OnlineMatchContext.HasOpponentChallenge
            || !Application.CanStreamedLevelBeLoaded(OnlineLevelSceneName))
        {
            return;
        }

        transitioning = true;
        SetPlayEnabled(false);
        SceneManager.LoadScene(OnlineLevelSceneName);
    }

    private void LeaveMatch()
    {
        if (leaving || transitioning)
        {
            return;
        }

        leaving = true;
        SetPlayEnabled(false);

        if (leaveButton != null)
        {
            leaveButton.interactable = false;
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

    private void SetPlayEnabled(bool enabled)
    {
        if (playButton != null)
        {
            playButton.interactable = enabled;
        }
    }

    private void SetStatus(string message)
    {
        SetText(statusText, message);
    }

    private static void SetText(Text text, string value)
    {
        if (text != null)
        {
            text.text = value;
        }
    }
}
