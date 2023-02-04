import logging
import numpy as np
from functools import partial

from rl_agents.agents.common.factory import preprocess_env, safe_deepcopy_env
from rl_agents.agents.tree_search.abstract import Node, AbstractTreeSearchAgent, AbstractPlanner
from rl_agents.agents.tree_search.olop import OLOP
from highway_env.vehicle.behavior import IDMVehicle
from highway_env.envs.common.action import action_factory

logger = logging.getLogger(__name__)


class MSLAGridAgent(AbstractTreeSearchAgent):
    """
        An agent that uses One Step Look Ahead to plan a sequence of action in an MDP.
    """
    def make_planner(self):
        return MSLAGrid(self.env, self.config)

    @classmethod
    def default_config(cls):
        config = super().default_config()
        config.update({
            "horizon": 10, # 10,
            "episodes": 5,
            "gamma": 0.99,
            "env_preprocessors": []
         })
        return config


class MSLAGrid(AbstractPlanner):
    """
       An implementation of One Step Look Ahead, with Upper Confidence Tree exploration.
    """
    def __init__(self, env, config=None):
        """
            New MSLAGrid instance.

        :param config: the MSLAGrid configuration. Use default if None.
        """
        super().__init__(config)
        self.env = env

    @classmethod
    def default_config(cls):
        cfg = super(MSLAGrid, cls).default_config()
        cfg.update({
            "temperature": 0,
            "closed_loop": False
        })
        return cfg

    def reset(self):
        self.root = MSLAGridNode(parent=None, planner=self)

    def run(self, i):
        """
            Run an iteration of One Step Look Ahead, starting from a given state

        :param i: the action.
        """
        depth = 0
        node = self.root
        total_reward = 0
        terminal = False
        action = i
        state_0 = self.mdp.state
        state_1 = self.mdp.transition[state_0, action]
        total_reward += self.config["gamma"] ** depth * self.mdp.reward[state_1, action]
        node_observation = observation if self.config["closed_loop"] else None
        node.expand_simple(action)
        node = node.get_child(action, observation=node_observation)
        depth = 1
        if self.mdp.ttc[state_1] < 1:
            return
        for j in range(0, 5):
            action = j
            state_2 = self.mdp.transition[state_1, action]
            total_reward_1 = total_reward + self.config["gamma"] ** depth * self.mdp.reward[state_2, action]
            node_observation = observation if self.config["closed_loop"] else None
            node.expand_simple(action)
            node2 = node.get_child(action, observation=node_observation)
            depth = 2
            if self.mdp.ttc[state_2] < 1:
                continue
            for k in range(0, 5):
                action = k
                state = self.mdp.transition[state_2, action]
                total_reward_2 = total_reward_1 + self.config["gamma"] ** depth * self.mdp.reward[state, action]
                node_observation = observation if self.config["closed_loop"] else None
                node2.expand_simple(action)
                node3 = node2.get_child(action, observation=node_observation)
                depth = 3
                if self.mdp.ttc[state] < 1:
                    continue
                total_reward_2 = self.evaluate(state, total_reward_2, depth=depth)
                node3.update(total_reward_2)
                if k == 0 or node2.value < node3.value:
                    node2.update(total_reward_2)
                    if j == 0 or node.value < node2.value:
                        node.update(total_reward_2)
                        if i == 0 or self.root.value < node.value:
                            self.root.update(total_reward_2)

    def evaluate(self, state, total_reward=0, depth=0):
        """
            Run the rollout policy to yield a sample of the value of being in a given state.

        :param state: the leaf state.
        :param total_reward: the initial total reward accumulated until now
        :param depth: the initial simulation depth
        :return: the total reward of the rollout trajectory
        """

        for h in range(depth, self.config["horizon"]):
            action = np.argmax(self.mdp.ttc[self.mdp.transition[state, range(self.mdp.transition.shape[1])]])
            state = self.mdp.transition[state, action]
            total_reward += self.config["gamma"] ** h * self.mdp.reward[state, action]
            if self.mdp.ttc[state] < 1:
                break
        return total_reward

    def plan(self, state, observation):
        self.reset()
        self.mdp = self.env.unwrapped.to_finite_mdp()
        for i in range(self.config['episodes']):
            self.run(i)
        return self.get_plan()

    def step_planner(self, action):
        if self.config["step_strategy"] == "prior":
            self.step_by_prior(action)
        else:
            super().step_planner(action)

    def step_by_prior(self, action):
        """
            Replace the MSLAGrid tree by its subtree corresponding to the chosen action, but also convert the visit counts
            to prior probabilities and before resetting them.

        :param action: a chosen action from the root node
        """
        self.step_by_subtree(action)
        # self.root.convert_visits_to_prior_in_branch()


class MSLAGridNode(Node):
    K = 1.0
    """ The value function first-order filter gain"""

    def __init__(self, parent, planner, prior=1):
        super(MSLAGridNode, self).__init__(parent, planner)
        self.value = 0
        self.prior = prior

    def selection_rule(self):
        if not self.children:
            return None
        # Tie best counts by best value
        actions = list(self.children.keys())
        counts = Node.all_argmax([self.children[a].count for a in actions])
        return actions[max(counts, key=(lambda i: self.children[actions[i]].get_value()))]

    def sampling_rule(self, temperature=None):
        """
            Select an action from the node.
            - if exploration is wanted with some temperature, follow the selection strategy.
            - else, select the action with maximum visit count

        :param temperature: the exploration parameter, positive or zero
        :return: the selected action
        """
        if self.children:
            actions = list(self.children.keys())
            # Randomly tie best candidates with respect to selection strategy
            indexes = [self.children[a].selection_strategy(temperature) for a in actions]
            return actions[self.random_argmax(indexes)]
        else:
            return None

    def expand_simple(self, action):
        """
            Expand a leaf node by creating a new child for given action.

        :param action: action for the desired children node
        """
        self.children[action] = type(self)(self, self.planner)

    def update(self, total_reward):
        """
            Update the visit count and value of this node, given a sample of total reward.

        :param total_reward: the total reward obtained through a trajectory passing by this node
        """
        self.value = total_reward

    def update_branch(self, total_reward):
        """
            Update the whole branch from this node to the root with the total reward of the corresponding trajectory.

        :param total_reward: the total reward obtained through a trajectory passing by this node
        """
        self.update(total_reward)
        if self.parent:
            self.parent.update_branch(total_reward)

    def get_child(self, action, observation=None):
        child = self.children[action]
        if observation is not None:
            if str(observation) not in child.children:
                child.children[str(observation)] = MSLAGridNode(parent=child, planner=self.planner, prior=0)
            child = child.children[str(observation)]
        return child

    def selection_strategy(self, temperature):
        """
            Select an action according to its value, prior probability and visit count.

        :param temperature: the exploration parameter, positive or zero.
        :return: the selected action with maximum value and exploration bonus.
        """
        if not self.parent:
            return self.get_value()

        return self.get_value() + temperature * len(self.parent.children) * self.prior/(self.count+1)

    def convert_visits_to_prior_in_branch(self, regularization=0.5):
        """
            For any node in the subtree, convert the distribution of all children visit counts to prior
            probabilities, and reset the visit counts.

        :param regularization: in [0, 1], used to add some probability mass to all children.
                               when 0, the prior is a Boltzmann distribution of visit counts
                               when 1, the prior is a uniform distribution
        """
        self.count = 0
        total_count = sum([(child.count+1) for child in self.children.values()])
        for child in self.children.values():
            child.prior = (1 - regularization)*(child.count+1)/total_count + regularization/len(self.children)
            child.convert_visits_to_prior_in_branch()

    def get_value(self):
        return self.value

