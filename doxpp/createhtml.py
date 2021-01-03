# dox++
# Copyright 2020-2021, Cris Luengo
#
# This file is part of dox++.  dox++ is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# Some bits of code are taken from m.css:
#   Copyright 2017-2020 Vladimír Vondruš <mosra@centrum.cz>
#   Copyright 2020 Yuri Edward <nicolas1.fraysse@epitech.eu>
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the "Software"),
#   to deal in the Software without restriction, including without limitation
#   the rights to use, copy, modify, merge, publish, distribute, sublicense,
#   and/or sell copies of the Software, and to permit persons to whom the
#   Software is furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included
#   in all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
#   THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#   FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#   DEALINGS IN THE SOFTWARE.

import os
import urllib.parse
import html
import mimetypes
import shutil

import markdown
import jinja2

from . import log
from . import walktree
from . import members

from .search import CssClass, ResultFlag, ResultMap, Trie, serialize_search_data, base85encode_search_data, search_filename, searchdata_filename, searchdata_filename_b85, searchdata_format_version

from .markdown.admonition import AdmonitionExtension
from .markdown.fix_links import FixLinksExtension
from .markdown.record_images import RecordLinkedImagesExtension
from .markdown.mdx_subscript import SubscriptExtension
from .markdown.mdx_superscript import SuperscriptExtension

doxpp_path = os.path.dirname(os.path.realpath(__file__))
default_templates = os.path.join(doxpp_path, 'html_templates')


class Status:
    # This defines the status of our generator
    def __init__(self, data, options):
        # Data read in from JSON file
        self.data = data

        # These dictionaries contain the same dictionaries as in 'data', but indexed by their ID so they're
        # easy to find. It is the *same* dictionaries, modifying these will modify 'data'.
        self.members = walktree.create_member_dict(data['members'])
        self.headers = walktree.create_element_dict(data['headers'])
        self.groups = walktree.create_element_dict(data['groups'])
        self.pages = walktree.create_element_dict(data['pages'])

        # This dictionary links each unique ID to a page where it can be found.
        # To link to an ID, link to "<page>.html#<ID>", unless page==ID, in which case
        # it suffices to link to "<page>.html".
        # Items not in here are not documented.
        self.id_map = {
            'classes': 'classes',
            'files': 'files',
            'index': 'index',
            'modules': 'modules',
            'namespaces': 'namespaces',
            'pages': 'pages'
        }

        # This dictionary links page_id to the compound with the relevant information for that page.
        self.html_pages = {}

        # This is the set of images referenced in the documentation
        self.images = set()

        # Options
        self.show_private = options['show_private']
        self.show_protected = options['show_protected']
        self.show_undocumented = options['show_undocumented']

    def get_link(self, id):
        # Convert an ID into a URL to link to
        try:
            page = self.id_map[id]
        except KeyError:
            log.error('Referencing type %s, which was excluded from documentation', id)
            return '#id'
        if page == id:
            return page + '.html'
        else:
            return page + '.html#' + id

    def find_title(self, id):
        standard = {
            'classes': 'Classes',
            'files': 'Files',
            'index': 'Home',
            'modules': 'Modules',
            'namespaces': 'Namespaces',
            'pages': 'Pages'
        }
        if id in standard:
            return standard[id]
        if id in self.members:
            return self.members[id]['name']
        if id in self.headers:
            return self.headers[id]['name']
        if id in self.groups:
            return self.groups[id]['name']
        if id in self.pages:
            return self.pages[id]['title']
        # TODO: how about links to (sub-)sections and anchors?
        return '(unknown)'

    def get_compound(self, id):
        if id in self.members:
            return self.members[id]
        elif id in self.headers:
            return self.headers[id]
        elif id in self.groups:
            return self.groups[id]
        elif id in self.pages:
            return self.pages[id]
        else:
            #log.error("Looking for 'compound' data structure for unknown id = %s", id)
            return {}


def generate_fully_qualified_names(members, status: Status):
    for member in members:
        name = member['name']
        if member['parent']:
            name = status.members[member['parent']]['fully_qualified_name'] + '::<wbr />' + name
        member['fully_qualified_name'] = name
        if 'members' in member:
            generate_fully_qualified_names(member['members'], status)


def register_anchors_to_page(compound, page_id, status: Status):
    for section in compound['sections']:
        status.id_map[section[0]] = page_id
    for anchor in compound['anchors']:
        status.id_map[anchor] = page_id

def document_member_on_page(member, page_id, status: Status):
    member['page_id'] = page_id
    status.id_map[member['id']] = page_id
    register_anchors_to_page(member, page_id, status)

def create_page(compound, status: Status):
    page_id = compound['id']
    compound['page_id'] = page_id
    status.id_map[page_id] = page_id
    register_anchors_to_page(compound, page_id, status)
    status.html_pages[page_id] = compound

def show_member(member, status: Status):
    if not (status.show_undocumented or member['brief'] or (member['member_type'] == 'enum' and member['has_value_details'])):
        return False
    if not status.show_private and 'access' in member and member['access'] == 'private':
        return False
    if not status.show_protected and 'access' in member and member['access'] == 'protected':
        return False
    return True

def has_documented_members(compound):
    return compound['has_enum_details'] or compound['has_alias_details'] or compound['has_function_details'] \
           or compound['has_variable_details'] or compound['has_macro_details']

def add_compound_member_booleans(compound):
    compound['has_enum_details'] = False
    compound['has_alias_details'] = False
    compound['has_function_details'] = False
    compound['has_variable_details'] = False
    compound['has_macro_details'] = False

def add_class_member_lists(compound):
    compound['typeless_functions'] = []
    compound['groups'] = []
    compound['groups_names'] = {}
    compound['classes'] = []
    compound['enums'] = []
    compound['aliases'] = []
    compound['functions'] = []
    compound['variables'] = []
    compound['related'] = []
    add_compound_member_booleans(compound)

def add_compound_member_lists(compound):
    compound['modules'] = []
    compound['namespaces'] = []
    compound['classes'] = []
    compound['enums'] = []
    compound['aliases'] = []
    compound['functions'] = []
    compound['variables'] = []
    compound['macros'] = []
    add_compound_member_booleans(compound)

def process_enum_values(member, status: Status):
    # member must be an enum
    # We only get here if member is being shown
    for value in member['members']:
        document_member_on_page(value, member['page_id'], status)

def process_class_member(compound, member, status: Status):
    # compound must be a class/struct/union
    if member['member_type'] in ['class', 'struct', 'union']:
        process_class(member, status)
        if 'page_id' in member:
            compound['classes'].append(member)
    elif show_member(member, status):
        document_member_on_page(member, compound['id'], status)
        member_type = member['member_type']
        if member['group']:
            if member['group'] not in compound['groups_names']:
                compound['groups_names'][member['group']] = len(compound['groups'])
                compound['groups'].append({
                    'name': member['group'],
                    'id': 'group--' + urllib.parse.quote(member['group']),
                    'members': []
                })
            compound['groups'][compound['groups_names'][member['group']]]['members'].append(member)
        elif 'method_type' in member and member['method_type'] != 'method':
            compound['typeless_functions'].append(member)
        elif member_type == 'enum':
            compound['enums'].append(member)
            process_enum_values(member, status)
        elif member_type == 'alias':
            compound['aliases'].append(member)
        elif member_type == 'function':
            compound['functions'].append(member)
        elif member_type == 'variable':
            compound['variables'].append(member)
        else:
            log.error("Member of type %s cannot be listed on %s page", member_type, compound['member_type'])
            return
        if member['doc'] or (member_type == 'enum' and member['has_value_details']):
            compound['has_' + member_type + '_details'] = True

def process_namespace_member(compound, member, status: Status):
    # compound must be a namespace
    if member['member_type'] != 'namespace' and not member['header']:
        # TODO: This only happens because of an issue with parent template class defined in other header file
        log.error("Member %s doesn't have a header file, this is a bug in dox++parse that needs to be fixed", member['id'])
    header_compound = status.headers[member['header']] if member['header'] else {}
    group_compound = status.groups[member['group']] if member['group'] else {}
    if member['member_type'] in ['class', 'struct', 'union']:
        process_class(member, status)
        if 'page_id' in member:
            compound['classes'].append(member)
            if header_compound:
                header_compound['classes'].append(member)
            if group_compound:
                group_compound['classes'].append(member)
    elif member['member_type'] == 'namespace':
        process_namespace(member, status)
        if 'page_id' in member:
            compound['namespaces'].append(member)
            if header_compound:
                header_compound['namespaces'].append(member)
            if group_compound:
                group_compound['namespaces'].append(member)
    elif show_member(member, status):
        if member['relates']:
            page_id = member['relates']
            status.get_compound(page_id)['related'].append(member)
        elif member['group']:
            page_id = member['group']
        elif compound['id']:
            page_id = compound['id']
        else:
            page_id = member['header']
        document_member_on_page(member, page_id, status)
        member_type = member['member_type']
        if member_type == 'enum':
            compound['enums'].append(member)
            if header_compound:
                header_compound['enums'].append(member)
            if group_compound:
                group_compound['enums'].append(member)
            process_enum_values(member, status)
        elif member_type == 'alias':
            compound['aliases'].append(member)
            if header_compound:
                header_compound['aliases'].append(member)
            if group_compound:
                group_compound['aliases'].append(member)
        elif member_type == 'function':
            compound['functions'].append(member)
            if header_compound:
                header_compound['functions'].append(member)
            if group_compound:
                group_compound['functions'].append(member)
        elif member_type == 'variable':
            compound['variables'].append(member)
            if header_compound:
                header_compound['variables'].append(member)
            if group_compound:
                group_compound['variables'].append(member)
        elif member_type == 'macro':
            compound['macros'].append(member)
            if header_compound:
                header_compound['macros'].append(member)
            if group_compound:
                group_compound['macros'].append(member)
        else:
            log.error("Member of type %s cannot be listed on %s page", member_type, compound['member_type'])
            return
        if member['doc'] or (member_type == 'enum' and member['has_value_details']):
            (compound if page_id == compound['id'] else status.get_compound(page_id))['has_' + member_type + '_details'] = True

def process_class(compound, status: Status):
    # compound must be a class/struct/union
    add_class_member_lists(compound)
    # Process namespace members
    for member in compound['members']:
        process_class_member(compound, member, status)
    # Create a page for this one?
    if compound['id'] and (show_member(compound, status) or has_documented_members(compound)):
        create_page(compound, status)

def process_namespace(compound, status: Status):
    # compound must be a namespace
    add_compound_member_lists(compound)
    # Process namespace members
    for member in compound['members']:
        process_namespace_member(compound, member, status)
    # Create a page for this one?
    if compound['id'] and (show_member(compound, status) or has_documented_members(compound)):
        create_page(compound, status)

def has_value_details(member):
    for child in member['members']:
        if child['brief']:
            return True
    return False

def verify_namespace_header(member):
    for child in member['members']:
        if child['header'] and child['header'] != member['header']:
            member['header'] = ''
            return

def find_header_file(compound):
    # TODO: Modules without members, that only have submodules, should be assigned a header file.
    # We first count how many members have the same header file
    headers = {}
    for members in (compound['classes'], compound['enums'], compound['aliases'],
                    compound['functions'], compound['variables'], compound['macros']):
        for member in members:
            if member['header'] in headers:
                headers[member['header']] += 1
            else:
                headers[member['header']] = 1
    # Any header file that is shown in 80% of the members we take as the header for the compound
    threshold = 0.8 * sum(headers.values())
    compound_header = max(headers, key=lambda m: headers[m], default='')
    if compound_header in headers and headers[compound_header] < threshold:
        compound_header = ''
    compound['header'] = compound_header

def assign_page(status: Status):
    # TODO: Add option `show_if_documented_children`
    # All header files will have a page, whether they're documented or not
    for header in status.headers.values():
        add_compound_member_lists(header)
        header['member_type'] = 'file'
        create_page(header, status)
    # All groups (modules) will have a page, whether they're documented or not
    for group in status.groups.values():
        add_compound_member_lists(group)
        group['member_type'] = 'module'
        create_page(group, status)
    # Assign sub-groups to be shown in parent group's page; they're always sorted
    for group in status.groups.values():
        group['modules'] = [status.groups[id] for id in group['subgroups']]
        group['modules'].sort(key=lambda x: x['name'].casefold())
    # Prepare class and namespace members with needed field. Also:
    #  - find out if enum members have documented values
    #  - find out if namespace members all have the same header file, and reset namespace header if not
    for member in status.members.values():
        if not member['id']:
            continue
        if member['member_type'] == 'namespace':
            add_compound_member_lists(member)
            verify_namespace_header(member)
        elif member['member_type'] in ['class', 'struct', 'union']:
            add_class_member_lists(member)
        elif member['member_type'] == 'enum':
            member['has_value_details'] = has_value_details(member)
    # Assign members to a specific page
    base = {  # bogus namespace to get things rolling
        'id': '',
        'member_type': 'namespace',
        'members': status.data['members'],
        'header': '',
        'group': ''
    }
    process_namespace(base, status)
    # Assign a header file to groups and namespaces
    for group in status.groups.values():
        find_header_file(group)
    for member in status.members.values():
        if member['id'] and member['member_type'] == 'namespace':
            find_header_file(member)
    # All pages have a page, obviously
    for page in status.pages.values():
        page['member_type'] = 'page'
        create_page(page, status)


def create_indices(status: Status):
    index = {}
    index['symbols'] = []   # Used in classes and namespaces index
    index['files'] = []     # Used in files index
    index['modules'] = []   # Used in modules index
    index['pages'] = []     # used in pages index
    # Symbols (class, struct, union and namespace)
    for member in status.members.values():
        if not member['id']:
            continue
        if member['member_type'] not in ['namespace', 'class', 'struct', 'union']:
            continue
        member['children'] = []
        if 'page_id' not in member or not member['page_id']:
            continue
        if member['parent']:
            parent = status.members[member['parent']]
            parent['children'].append(member) # Parent has been processed earlier, has a 'children' key
            if member['member_type'] == 'namespace':
                parent['has_child_namespace'] = True
        else:
            index['symbols'].append(member)
    index['symbols'].sort(key=lambda x: x['name'].casefold())
    for member in status.members.values():
        if 'children' in member:
            member['children'].sort(key=lambda x: x['name'].casefold())
    # Files (headers)
    index['files'] = walktree.build_file_hierarchy(status.data['headers'])
    # Modules (groups)
    status.data['groups'].sort(key=lambda x: x['name'].casefold())
    for group in status.data['groups']:
        if not group['parent']:
            index['modules'].append(group)
        group['children'] = group['modules']
    # Pages
    status.data['pages'].sort(key=lambda x: x['title'].casefold())
    for page in status.data['pages']:
        if page['id'] == 'index':
            continue  # the index is not on the list of pages
        if not page['parent']:
            index['pages'].append(page)
        page['children'] = []
        for s in page['subpages']:
            page['children'].append(status.pages[s])  # We don't sort these, but use them in the order they're referenced in the parent page
    return index


def process_navbar_links(navbar, status: Status):
    # Convert (title, id, sub) into (title, link, id, sub)
    out = []
    for title, id, sub in navbar:
        if sub:
            sub = process_navbar_links(sub, status)
        link = status.get_link(id)
        if not title:
            title = status.find_title(id)
        out.append((title, link, id, sub))
    return out


def remove_p_tag(title):
    # Markdown always encloses its output in paragraph tags, but we don't want these when it's
    # a page or section title, nor for the brief docs.
    if title[:3] == '<p>' and title[-4:] == '</p>':
        title = title[3:-4]
    if '<p>' in title:
        log.warning("Title contains a HTML paragraph tag: %s", title)
    return title

def process_sections_recursive(sections, level, md):
    if sections[0][2] < level:
        return []
    section = sections.pop(0)
    subsections = []
    while sections and sections[0][2] > section[2]:
        subsections.append(process_sections_recursive(sections, level + 1, md))
    return (section[0], remove_p_tag(md.reset().convert(section[1])), subsections)

def process_sections(compound, md):
    # compound['sections'] is a flat list of tuples [(name, title, level), ...]
    # We turn it into a hierarchical list according to level: [(name, title, [...]), ...]
    # We also apply Markdown processing to `title`
    sections = compound['sections']
    compound['sections'] = []
    while sections:
        compound['sections'].append(process_sections_recursive(sections, 1, md))

def parse_markdown(status: Status):
    extensions = [
        # Extensions packaged with `markdown`:
        'attr_list',        # https://python-markdown.github.io/extensions/attr_list/
        'md_in_html',       # https://python-markdown.github.io/extensions/md_in_html/
        'tables',           # https://python-markdown.github.io/extensions/tables/
        'fenced_code',      # https://python-markdown.github.io/extensions/fenced_code_blocks/
        'codehilite',       # https://python-markdown.github.io/extensions/code_hilite/
        'sane_lists',       # https://python-markdown.github.io/extensions/sane_lists/
        'smarty',           # https://python-markdown.github.io/extensions/smarty/
        # Installed with package `markdown-headdown`
        'mdx_headdown',     # https://github.com/SaschaCowley/Markdown-Headdown
        # Our own concoctions
        AdmonitionExtension(),              # Modification of the standard 'admonition' extension
        FixLinksExtension(status.id_map),   # Fixes links from '#id' to 'page_id.html#id'
        RecordLinkedImagesExtension(status.images),  # Stores names of images linked in the documentation
        # Two extensions not installed through PyPI because they cause a downgrade of the Markdown package
        # (would be installed with packages `MarkdownSuperscript` and `MarkdownSubscript`)
        SubscriptExtension(),       # https://github.com/jambonrose/markdown_subscript_extension
        SuperscriptExtension()      # https://github.com/jambonrose/markdown_superscript_extension
    ]
    extension_configs = {
        'codehilite': {
            'css_class': 'm-code'
        },
        'mdx_headdown': {
            'offset': 1
        }
    }
    # TODO: Create an extension that adds image file names to status.images
    # TODO: Create a LaTeX math extension based on some stuff in m.css as well as the following:
    #       https://github.com/justinvh/Markdown-LaTeX
    #       https://github.com/ShadowKyogre/python-asciimathml
    md = markdown.Markdown(extensions=extensions, extension_configs=extension_configs, output_format="html5")

    for header in status.headers.values():
        if header['brief']:
            header['brief'] = remove_p_tag(md.reset().convert(header['brief']))
        if header['doc']:
            header['doc'] = md.reset().convert(header['doc'])
        process_sections(header, md)
    for group in status.groups.values():
        if group['brief']:
            group['brief'] = remove_p_tag(md.reset().convert(group['brief']))
        if group['doc']:
            group['doc'] = md.reset().convert(group['doc'])
        process_sections(group, md)
    for member in status.members.values():
        if member['id'] not in status.id_map:
            continue
        if member['brief']:
            member['brief'] = remove_p_tag(md.reset().convert(member['brief']))
        if member['doc']:
            member['doc'] = md.reset().convert(member['doc'])
        process_sections(member, md)
    for page in status.pages.values():
        if page['doc']:
            page['doc'] = md.reset().convert(page['doc'])
        if page['title']:
            page['title'] = remove_p_tag(md.reset().convert(page['title']))
        process_sections(page, md)


def render_type(type, status: Status, doc_link_class):
    typename = html.escape(type['typename'])
    if not typename:
        return
    if type['id']:
        typename = '<a href="' + status.get_link(type['id']) + '" class="' + doc_link_class + '">' + typename + '</a>'
    if type['qualifiers']:
        if type['qualifiers'][0] == 'c':
            typename += ' '
        typename += html.escape(type['qualifiers'])
    type['typename'] = typename

def parse_types(status: Status, doc_link_class):
    for member in status.members.values():
        if 'type' in member and isinstance(member['type'], dict):
            render_type(member['type'], status, doc_link_class)
        if 'return_type' in member and isinstance(member['return_type'], dict):
            render_type(member['return_type'], status, doc_link_class)
        if 'arguments' in member:
            for arg in member['arguments']:
                render_type(arg, status, doc_link_class)
        if 'template_parameters' in member:
            for arg in member['template_parameters']:
                if arg['type'] == 'type':
                    arg['type'] = 'typename'
                    if arg['default']:
                        render_type(arg['default'], status, doc_link_class)
                else:  # isinstance(arg['type'], 'dict'):
                    render_type(arg['type'], status, doc_link_class)
                arg['name'] = html.escape(arg['name'])  # just in case this is "<SFINAE>".


def add_wbr(text: str):
    if '<' in text:  # Stuff contains HTML code, do not touch!
        return text
    if '::' in text:  # C++ names
        return text.replace('::', '::<wbr />')
    if '_' in text:  # VERY_LONG_UPPER_CASE macro names
        return text.replace('_', '_<wbr />')
    # These characters are quite common, so at least check that there is no
    # space (which may hint that the text is actually some human language):
    if '/' in text and not ' ' in text:  # URLs
        return text.replace('/', '/<wbr />')
    return text


def add_breadcrumb(compound, name, compounds):
    path_reverse = [compound['id']]
    parent = compound['parent']
    while parent:
        path_reverse.append(parent)
        parent = compounds[parent]['parent']
    compound['breadcrumb'] = []
    for elem in reversed(path_reverse):
        compound['breadcrumb'].append((compounds[elem][name], elem + '.html'))


def fixup_namespace_compound_members(compound, status: Status):
    # Adjusts data in the members referenced in compound for display in the context of compound.
    # Some details about a member appear differently depending on which page we're looking at.
    # This function only changes member fields that are not primary, (i.e. original data is not lost):
    # - has_details: if a section with member details needs to be shown for this member
    # - include: either () to not show include file information, or ("name", link) to show it
    # - TODO: fixup referenced types in each member to be relative to compound if compound is a namespace
    def has_details(member):
        return member['page_id'] == compound['page_id'] and \
               (len(member['doc']) > 0 or (member['member_type'] == 'enum' and member['has_value_details']))
    if compound['member_type'] == 'file':
        # None of the members will show an include file
        compound_header = compound['id']
    else:
        compound_header = compound['header']
        if compound_header:
            compound['include'] = ('"' + status.headers[compound_header]['name'] + '"', compound_header + '.html')
        else:
            compound['include'] = ()
    for members in (compound['enums'], compound['aliases'], compound['functions'],
                    compound['variables'], compound['macros']):
        for member in members:
            member['has_details'] = has_details(member)
            if member['header'] == compound_header:
                member['include'] = ()
            else:
                member['include'] = ('"' + status.headers[member['header']]['name'] + '"', member['header'] + '.html')
                member['has_details'] = True
                if member['page_id'] == compound['id']:
                    compound['has_' + member['member_type'] + '_details'] = True

def fixup_class_compound_members(compound, status: Status):
    # This function works like `fixup_namespace_compound_members`, but is for when the compound is a class/struct/union.
    def has_details(member):
        return len(member['doc']) > 0 or (member['member_type'] == 'enum' and member['has_value_details'])
    compound['include'] = ('"' + status.headers[compound['header']]['name'] + '"', compound['header'] + '.html')
    for members in (compound['typeless_functions'], compound['enums'], compound['aliases'], compound['functions'],
                    compound['variables']):
        for member in members:
            member['has_details'] = has_details(member)
    for group in compound['groups']:
        for member in group['members']:
            member['has_details'] = has_details(member)
    for member in compound['related']:
        member['has_details'] = has_details(member)
        if member['header'] == compound['header']:
            member['include'] = ()
        else:
            member['include'] = ('"' + status.headers[member['header']]['name'] + '"', member['header'] + '.html')
            member['has_details'] = True
            compound['has_' + member['member_type'] + '_details'] = True


def createhtml(input_file, output_dir, options, template_params):
    """
    Generates HTML pages for the documentation in the JSON file `input_file`.

    :param input_file: the name of the JSON file that contains the documentation to format (string)
    :param output_dir: directory where the HTML files will be written (string)
    :param options: dictionary with options for how to process things
    :param template_params: dictionary with template parameters

    Options must contain the keys:
    - 'show_private': include private members in the documentation
    - 'show_protected': include protected members in the documentation
    - 'show_undocumented': include undocumented members in the documentation
    - 'extra_files': list of extra files to copy to the output directory
    - 'templates': path to templates to use instead of default ones
    - 'source_files': list of source files (header files + markdown files), used to locate
                      image files referenced in documentation.
    """

    # Load data
    status = Status(walktree.load_data_from_json_file(input_file), options)

    # Generate fully qualified names
    generate_fully_qualified_names(status.data['members'], status)

    # Find out which pages to create, what is listed in each, and in which page
    # the detailed documentation for each member has to go
    assign_page(status)
    #print('\n\nhtml_pages', status.html_pages)
    #print('\n\nid_map', status.id_map)

    # Parse all Markdown
    parse_markdown(status)

    # Convert type name strings into HTML links if appropriate
    parse_types(status, template_params['DOC_LINK_CLASS'])

    # Create tree structure for index pages
    index = create_indices(status)

    # We need to have an index.html page
    if 'index' not in status.pages:
        page = members.new_page('index', template_params['PROJECT_NAME'], '')
        status.pages['index'] = page

    # Navbar links
    if 'LINKS_NAVBAR1' in template_params:
        template_params['LINKS_NAVBAR1'] = process_navbar_links(template_params['LINKS_NAVBAR1'], status)
    if 'LINKS_NAVBAR2' in template_params:
        template_params['LINKS_NAVBAR2'] = process_navbar_links(template_params['LINKS_NAVBAR2'], status)

    # If no stylesheets were given, use the default one
    if not 'STYLESHEETS' in template_params or not template_params['STYLESHEETS']:
        template_params['STYLESHEETS'] = ['css/m-light-documentation.compiled.css']

    # Fill in default favicon if not given, and get type
    if not template_params['FAVICON']:
        template_params['FAVICON'] = 'html_templates/favicon-light.png'
    template_params['FAVICON'] = (template_params['FAVICON'], mimetypes.guess_type(template_params['FAVICON'])[0])

    # If custom template dir was supplied, use the default template directory as a fallback
    template_paths = [options['templates']]
    if options['templates'] != default_templates:
        template_paths.append(default_templates)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_paths),
                             trim_blocks=True, lstrip_blocks=True, enable_async=True)

    # Filter to return file basename or the full URL, if absolute
    def basename_or_url(path):
        if urllib.parse.urlparse(path).netloc:
            return path
        return os.path.basename(path)
    def rtrim(value):
        return value.rstrip()
    env.filters['rtrim'] = rtrim
    env.filters['basename_or_url'] = basename_or_url
    env.filters['urljoin'] = urllib.parse.urljoin

    # Generate the html for the members
    for id in status.html_pages:
        file = id + '.html'
        compound = status.html_pages[id]
        #if not compound:
        #    log.error("Generating 'compound' data structure for unknown id = %s", id)
        #    continue
        type = compound['member_type']
        if type == 'file':
            compound['breadcrumb'] = [(compound['name'].replace('/', '/<wbr />'), file)]
            fixup_namespace_compound_members(compound, status)
        elif type == 'module':
            add_breadcrumb(compound, 'name', status.groups)
            fixup_namespace_compound_members(compound, status)
        elif type == 'page':
            add_breadcrumb(compound, 'title', status.pages)
        else:
            add_breadcrumb(compound, 'name', status.members)
            if type == 'namespace':
                fixup_namespace_compound_members(compound, status)
            else:
                fixup_class_compound_members(compound, status)
        template = env.get_template(type + '.html')
        rendered = template.render(compound=compound,
                                   FILENAME=file,
                                   SEARCHDATA_FORMAT_VERSION=searchdata_format_version,
                                   **template_params)
        with open(os.path.join(output_dir, file), 'w') as f:
            f.write(rendered)

    # Generate indexes for pages, groups (==modules), namespaces, classes/structs/unions (==classes), and headers (==files)
    for file in ['pages.html', 'modules.html', 'namespaces.html', 'classes.html', 'files.html']:
        template = env.get_template(file)
        rendered = template.render(index=index,
                                   FILENAME=file,
                                   SEARCHDATA_FORMAT_VERSION=searchdata_format_version,
                                   **template_params)
        with open(os.path.join(output_dir, file), 'w') as f:
            f.write(rendered)

    # Generate search data
    # TODO

    # Copy over all referenced files
    for i in template_params['STYLESHEETS'] + options['extra_files'] + ([template_params['PROJECT_LOGO']] if template_params['PROJECT_LOGO'] else []) + ([template_params['FAVICON'][0]] if template_params['FAVICON'][0] else []):
        if urllib.parse.urlparse(i).netloc:
            continue
        file_out = i
        # File is either found relative to the current directory or relative to script directory
        if not os.path.exists(i):
            i = os.path.join(doxpp_path, i)
        if not os.path.exists(i):
            log.error("File %s not found", file_out)
        log.info("Copying %s to output", i)
        shutil.copy(i, os.path.join(output_dir, os.path.basename(file_out)))
    # The images we need to search for in the input directories
    source_dirs = set()
    for s in options['source_files']:
        source_dirs.add(os.path.dirname(s))
    for i in status.images:
        found = False
        for s in source_dirs:
            p = os.path.join(s,i)
            if os.path.exists(p):
                shutil.copy(p, os.path.join(output_dir, os.path.basename(i)))
                found = True
                break
        if not found:
            log.error("File %s not found", i)
    # The search.js is special, we encode the version information into its filename
    if not template_params['SEARCH_DISABLED']:
        shutil.copy(os.path.join(doxpp_path, 'html_templates/search.js'), os.path.join(output_dir, os.path.basename(search_filename)))
