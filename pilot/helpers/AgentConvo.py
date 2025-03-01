import subprocess
from termcolor import colored

from database.database import get_development_step_from_hash_id, save_development_step, delete_all_subsequent_steps
from utils.utils import array_of_objects_to_string
from utils.llm_connection import get_prompt, create_gpt_chat_completion
from utils.utils import get_sys_message, find_role_from_step, capitalize_first_word_with_underscores
from logger.logger import logger
from prompts.prompts import ask_user
from const.llm import END_RESPONSE


class AgentConvo:
    def __init__(self, agent):
        self.messages = []
        self.branches = {}
        self.log_to_user = True
        self.agent = agent
        self.high_level_step = self.agent.project.current_step

        # add system message
        self.messages.append(get_sys_message(self.agent.role))

    def send_message(self, prompt_path=None, prompt_data=None, function_calls=None):
        # craft message
        self.construct_and_add_message_from_prompt(prompt_path, prompt_data)

        if function_calls is not None and 'function_calls' in function_calls:
            self.messages[-1]['content'] += '\nMAKE SURE THAT YOU RESPOND WITH A CORRECT JSON FORMAT!!!'

        # check if we already have the LLM response saved
        if self.agent.__class__.__name__ == 'Developer':
            self.agent.project.llm_req_num += 1
        development_step = get_development_step_from_hash_id(self.agent.project, prompt_path, prompt_data, self.agent.project.llm_req_num)
        if development_step is not None and self.agent.project.skip_steps:
            # if we do, use it
            print(colored(f'Restoring development step with id {development_step.id}', 'yellow'))
            self.agent.project.checkpoints['last_development_step'] = development_step
            self.agent.project.restore_files(development_step.id)
            response = development_step.llm_response
            self.messages = development_step.messages

            if self.agent.project.skip_until_dev_step and str(development_step.id) == self.agent.project.skip_until_dev_step:
                self.agent.project.skip_steps = False
                delete_all_subsequent_steps(self.agent.project)

                if 'delete_unrelated_steps' in self.agent.project.args and self.agent.project.args['delete_unrelated_steps']:
                    self.agent.project.delete_all_steps_except_current_branch()
        else:
            # if we don't, get the response from LLM
            response = create_gpt_chat_completion(self.messages, self.high_level_step, function_calls=function_calls)
            if self.agent.__class__.__name__ == 'Developer':
                development_step = save_development_step(self.agent.project, prompt_path, prompt_data, self.messages, response)
                self.agent.project.checkpoints['last_development_step'] = development_step
                self.agent.project.save_files_snapshot(development_step.id)

        # TODO handle errors from OpenAI
        if response == {}:
            raise Exception("OpenAI API error happened.")

        response = self.postprocess_response(response, function_calls)

        # TODO remove this once the database is set up properly
        message_content = response[0] if type(response) == tuple else response
        if isinstance(message_content, list):
            if 'to_message' in function_calls:
                string_response = function_calls['to_message'](message_content)
            elif len(message_content) > 0 and isinstance(message_content[0], dict):
                string_response = [
                    f'#{i}\n' + array_of_objects_to_string(d)
                    for i, d in enumerate(message_content)
                ]
            else:
                string_response = ['- ' + r for r in message_content]

            message_content = '\n'.join(string_response)
        # TODO END

        # TODO we need to specify the response when there is a function called
        # TODO maybe we can have a specific function that creates the GPT response from the function call
        self.messages.append({"role": "assistant", "content": message_content})
        self.log_message(message_content)

        return response

    def continuous_conversation(self, prompt_path, prompt_data, function_calls=None):
        self.log_to_user = False
        accepted_messages = []
        response = self.send_message(prompt_path, prompt_data, function_calls)

        # Continue conversation until GPT response equals END_RESPONSE
        while response != END_RESPONSE:
            print(colored("Do you want to add anything else? If not, ", 'yellow') + colored('just press ENTER.', 'yellow', attrs=['bold']))
            user_message = ask_user(self.agent.project, response, False)

            if user_message == "":
                accepted_messages.append(response)

            self.messages.append({"role": "user", "content": user_message})
            response = self.send_message(None, None, function_calls)

        self.log_to_user = True
        return accepted_messages

    def save_branch(self, branch_name):
        self.branches[branch_name] = self.messages.copy()

    def load_branch(self, branch_name):
        self.messages = self.branches[branch_name].copy()

    def convo_length(self):
        return len([msg for msg in self.messages if msg['role'] != 'system'])

    def postprocess_response(self, response, function_calls):
        if 'function_calls' in response and function_calls is not None:
            if 'send_convo' in function_calls:
                response['function_calls']['arguments']['convo']  = self
            response = function_calls['functions'][response['function_calls']['name']](**response['function_calls']['arguments'])
        elif 'text' in response:
            response = response['text']

        return response

    def log_message(self, content):
        print_msg = capitalize_first_word_with_underscores(self.high_level_step)
        if self.log_to_user:
            if self.agent.project.checkpoints['last_development_step'] is not None:
                print(colored("\nDev step ", 'yellow') + colored(self.agent.project.checkpoints['last_development_step'], 'yellow', attrs=['bold']) + '\n', end='')
            print(f"\n{content}\n")
        logger.info(f"{print_msg}: {content}\n")

    def to_playground(self):
        with open('const/convert_to_playground_convo.js', 'r', encoding='utf-8') as file:
            content = file.read()
        process = subprocess.Popen('pbcopy', stdin=subprocess.PIPE)
        process.communicate(content.replace('{{messages}}', str(self.messages)).encode('utf-8'))

    def remove_last_x_messages(self, x):
        self.messages = self.messages[:-x]

    def construct_and_add_message_from_prompt(self, prompt_path, prompt_data):
        if prompt_path is not None and prompt_data is not None:
            prompt = get_prompt(prompt_path, prompt_data)
            self.messages.append({"role": "user", "content": prompt})
