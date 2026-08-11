using UnityEngine;
using UnityEngine.Events;
using UnityEngine.UI;

public sealed class CoCreationStagePlayView : MonoBehaviour
{
    [SerializeField] private Button returnButton;
    [SerializeField] private Text returnButtonText;
    [SerializeField] private GameObject statusPanel;
    [SerializeField] private Text statusText;

    private UnityAction returnAction;

    private void Awake()
    {
        gameObject.SetActive(CoCreationPlayContext.IsActive);
    }

    public void Bind(bool useChinese, UnityAction onReturn)
    {
        gameObject.SetActive(true);
        Unbind();
        returnAction = onReturn;

        if (returnButtonText != null)
        {
            returnButtonText.text = useChinese
                ? "RETURN TO LAB / 返回共创工作台"
                : "RETURN TO CO-CREATION LAB";
        }

        if (returnButton != null && returnAction != null)
        {
            returnButton.onClick.AddListener(returnAction);
            returnButton.interactable = true;
        }

        SetStatus("");
    }

    public void Unbind()
    {
        if (returnButton != null && returnAction != null)
        {
            returnButton.onClick.RemoveListener(returnAction);
        }

        returnAction = null;
    }

    public void SetReturnInteractable(bool interactable)
    {
        if (returnButton != null)
        {
            returnButton.interactable = interactable;
        }
    }

    public void SetStatus(string message)
    {
        if (statusText != null)
        {
            statusText.text = message ?? "";
        }

        if (statusPanel != null)
        {
            statusPanel.SetActive(!string.IsNullOrWhiteSpace(message));
        }
    }
}
