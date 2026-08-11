using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class MatchBriefingController : MonoBehaviour
{
    private const string LobbySceneName = "Online_Lobby";
    private const string DraftSceneName = "Draft";

    private OnlineMatchClient client;
    private Button readyButton;
    private Button leaveButton;
    private Text readyButtonText;
    private Text youName;
    private Text opponentName;
    private Text youStatus;
    private Text opponentStatus;
    private Text youAvatarLetter;
    private Text opponentAvatarLetter;
    private Text footerHint;
    private bool readyRequestInFlight;
    private bool leavingScene;
    private bool transitioned;

    private void Start()
    {
        if (!OnlineMatchContext.HasMatch)
        {
            Debug.LogWarning(
                "MatchBriefingController: No active online room; returning to lobby."
            );
            SceneManager.LoadScene(LobbySceneName);
            return;
        }

        OnlineSceneUi.EnsureEventSystem();
        client = gameObject.AddComponent<OnlineMatchClient>();
        ResolveUi();
        WireUi();
        OnlineSceneUi.ConfigureRaycastTargets();
        UpdateUi(OnlineMatchContext.RoomState);
        StartCoroutine(PollRoom());
    }

    private void ResolveUi()
    {
        readyButton = OnlineSceneUi.EnsureButton("ReadyButton");
        leaveButton = OnlineSceneUi.EnsureButton("LeaveMatchButton");
        readyButtonText = OnlineSceneUi.FindText("ReadyButtonText");
        youName = OnlineSceneUi.FindText("YouName");
        opponentName = OnlineSceneUi.FindText("OpponentName");
        youStatus = OnlineSceneUi.FindText("YouStatus");
        opponentStatus = OnlineSceneUi.FindText("OpponentStatus");
        youAvatarLetter = OnlineSceneUi.FindText("YouAvatarLetter");
        opponentAvatarLetter =
            OnlineSceneUi.FindText("OpponentAvatarLetter");
        footerHint = OnlineSceneUi.FindText("FooterHint");
    }

    private void WireUi()
    {
        if (readyButton != null)
        {
            readyButton.onClick.AddListener(SetReady);
        }

        if (leaveButton != null)
        {
            leaveButton.onClick.AddListener(LeaveMatch);
        }
    }

    private void SetReady()
    {
        if (readyRequestInFlight || leavingScene || transitioned)
        {
            return;
        }

        OnlinePlayerState currentPlayer = OnlineMatchContext.RoomState
            ?.FindPlayer(OnlineMatchContext.PlayerNumber);

        if (currentPlayer != null && currentPlayer.ready)
        {
            return;
        }

        readyRequestInFlight = true;

        if (readyButton != null)
        {
            readyButton.interactable = false;
        }

        SetFooter("Sending ready state...");
        StartCoroutine(
            client.SetReady(
                true,
                state =>
                {
                    readyRequestInFlight = false;
                    OnlineMatchContext.ApplyState(state);
                    UpdateUi(state);
                    TryContinue(state);
                },
                error =>
                {
                    readyRequestInFlight = false;
                    SetFooter("Ready failed: " + error);
                    UpdateUi(OnlineMatchContext.RoomState);
                }
            )
        );
    }

    private IEnumerator PollRoom()
    {
        while (!leavingScene && !transitioned && OnlineMatchContext.HasMatch)
        {
            yield return client.GetRoom(
                state =>
                {
                    OnlineMatchContext.ApplyState(state);
                    UpdateUi(state);
                    TryContinue(state);
                },
                error => SetFooter("Connection error: " + error)
            );

            if (!leavingScene && !transitioned)
            {
                yield return new WaitForSecondsRealtime(1f);
            }
        }
    }

    private void TryContinue(OnlineRoomState state)
    {
        if (state == null
            || state.status != "waiting_for_challenges"
            || transitioned)
        {
            return;
        }

        transitioned = true;
        SetFooter("Both players are ready. Opening the initial draft step...");
        SceneManager.LoadScene(DraftSceneName);
    }

    private void LeaveMatch()
    {
        if (leavingScene || transitioned)
        {
            return;
        }

        leavingScene = true;

        if (readyButton != null)
        {
            readyButton.interactable = false;
        }

        if (leaveButton != null)
        {
            leaveButton.interactable = false;
        }

        SetFooter("Leaving match...");
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

    private void UpdateUi(OnlineRoomState state)
    {
        int currentNumber = OnlineMatchContext.PlayerNumber;
        int opponentNumber = currentNumber == 1 ? 2 : 1;
        OnlinePlayerState currentPlayer = state?.FindPlayer(currentNumber);
        OnlinePlayerState opponentPlayer = state?.FindPlayer(opponentNumber);
        bool cancelled = state != null && state.status == "cancelled";

        SetText(youName, "PLAYER " + PlayerLetter(currentNumber));
        SetText(opponentName, "PLAYER " + PlayerLetter(opponentNumber));
        SetText(youAvatarLetter, PlayerLetter(currentNumber));
        SetText(opponentAvatarLetter, PlayerLetter(opponentNumber));
        SetText(
            youStatus,
            currentPlayer != null && currentPlayer.ready
                ? "READY"
                : "WAITING FOR READY"
        );
        SetText(
            opponentStatus,
            opponentPlayer == null
                ? "CONNECTING..."
                : opponentPlayer.ready
                    ? "READY"
                    : "WAITING FOR READY"
        );

        bool alreadyReady = currentPlayer != null && currentPlayer.ready;

        if (readyButton != null)
        {
            readyButton.interactable =
                !readyRequestInFlight
                && !alreadyReady
                && !cancelled
                && opponentPlayer != null;
        }

        SetText(readyButtonText, alreadyReady ? "READY!" : "READY");

        if (cancelled)
        {
            SetFooter("Your opponent left. This match was cancelled.");
        }
        else if (alreadyReady)
        {
            SetFooter("Ready confirmed. Waiting for your opponent...");
        }
        else
        {
            SetFooter(
                "Room "
                + OnlineMatchContext.RoomCode
                + " — both players must be ready."
            );
        }
    }

    private void SetFooter(string message)
    {
        SetText(footerHint, message);
    }

    private static string PlayerLetter(int playerNumber)
    {
        return playerNumber == 2 ? "B" : "A";
    }

    private static void SetText(Text text, string value)
    {
        if (text != null)
        {
            text.text = value;
        }
    }
}
