import { LinearClient } from "@linear/sdk";

const linearClient = new LinearClient({
  apiKey: process.env.LINEAR_API_KEY,
});

async function getMyIssues() {
  const me = await linearClient.viewer;
  const myIssues = await me.assignedIssues();

  if (myIssues.nodes.length) {
    myIssues.nodes.map((issue) =>
      console.log(`${me.displayName} has issue: ${issue.title} ${issue.state}`)
    );
  } else {
    console.log(`${me.displayName} has no issues`);
  }
}

getMyIssues();
