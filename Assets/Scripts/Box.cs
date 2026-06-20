using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class Box : MonoBehaviour
{   
    public Transform start;
    public LevelManager LM;

    private bool isReached;
    public void Move(Vector3 dir)
    {
        transform.position += dir;
    }

    public void Awake()
    {
        transform.position = start.position;
    }

    private void Update()
    {
        Reach();
    }

    public void Reach()
    {
        if (isOnTarget() && !isReached)
        {
            isReached = true;
            LM.BoxReachTarget();
        }
        else if (!isOnTarget() && isReached)
        {
            isReached = false;
            LM.BoxLeaveTarget();
        }
    }

    public bool isOnTarget()
    {
        Collider2D[] hit = Physics2D.OverlapCircleAll(transform.position, 0.1f);

        foreach (Collider2D collider in hit)
        {
            if (collider.CompareTag("Target"))
            {
                return true;
            }
        }

        return false;
    }
}
